import { HttpClient } from '@angular/common/http';
import { DestroyRef, Injectable, computed, effect, inject, untracked } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { ChatStateService } from '../chat/chat-state.service';
import { HandoffView, isModelStep } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { WorkflowRunView } from './workflow-run.models';
import { isModelRunStep, stepRow } from './workflow-run-progress';

/** The first wait, and the longest, between two reads of one run (milliseconds). */
export const REFRESH_FIRST_MS = 5_000;
export const REFRESH_MAX_MS = 30_000;

/** Rows a step can sit in on the server while this tab has no stream for it. */
const SERVER_PENDING: ReadonlySet<HandoffView['status']> = new Set(['open', 'running', 'needs_input']);

interface Watch {
  sessionId: string;
  timer: ReturnType<typeof setTimeout> | null;
  /** The next wait; doubles after a read that moved nothing, back to the first after one that did. */
  delay: number;
}

/**
 * Keeps a workflow run's step rows current while a step runs on the server and not in this tab
 * (PR-D.5a's follow-up, `docs/specs/workflow-designer.md` §20.3).
 *
 * After a reload, a step that was streaming keeps running on the Runtime, but nothing in the tab
 * re-reads its row: its line said "Working on" until the next reload, and with stages a reader
 * queued behind it waited that long too. This reads the run (`GET …/workflow-runs/{runId}`, one
 * request a run per tick, never one per step) and feeds the rows through
 * `HandoffStateService.advanceRows`, which only moves a row forward and never touches one this tab
 * streams. The queue's own effect then starts any reader whose reads have settled.
 *
 * It reads only while it has to:
 * - a run of the conversation being viewed, or one with steps in this tab's queue,
 * - that is approved or stopped and has a kept step pending on the server (started, not finished)
 *   with no stream in this tab;
 * - every 5 s at first, doubling to 30 s while nothing changes;
 * - never while the page is hidden, and not again once the run settles.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunRefreshService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly runs = inject(WorkflowRunStateService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly queue = inject(WorkflowRunQueueService);

  private readonly watches = new Map<string, Watch>();
  private hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden';

  /** Run id → its source, for every run that has a step this tab can't see finish. */
  readonly stale = computed((): ReadonlyMap<string, string> => {
    const viewed = this.chatState.viewedSessionId();
    const sources = new Set<string>(viewed ? [viewed] : []);
    const out = new Map<string, string>();
    for (const source of this.sourcesWithRuns(sources)) {
      for (const run of this.runs.runsFor(source)) {
        if (source !== viewed && !this.queue.isQueued(run.runId)) continue;
        if (this.hasServerPendingStep(run)) out.set(run.runId, run.sessionId);
      }
    }
    return out;
  });

  constructor() {
    effect(() => {
      const stale = this.stale();
      untracked(() => this.reconcile(stale));
    });
    if (typeof document !== 'undefined') {
      const onVisibility = () => {
        this.hidden = document.visibilityState === 'hidden';
        if (this.hidden) {
          for (const watch of this.watches.values()) this.clearTimer(watch);
          return;
        }
        // Back in view: read each run once soon, from the first wait again.
        for (const [runId, watch] of this.watches) {
          watch.delay = REFRESH_FIRST_MS;
          this.schedule(runId, watch);
        }
      };
      document.addEventListener('visibilitychange', onVisibility);
      inject(DestroyRef).onDestroy(() => {
        document.removeEventListener('visibilitychange', onVisibility);
        for (const watch of this.watches.values()) this.clearTimer(watch);
        this.watches.clear();
      });
    }
  }

  /** The viewed conversation, plus every source with a run queued in this tab. */
  private sourcesWithRuns(viewed: Set<string>): Set<string> {
    const sources = new Set(viewed);
    for (const source of this.queue.queuedSources()) sources.add(source);
    return sources;
  }

  /**
   * A kept step this tab's copy shows started and unfinished, whose conversation isn't streaming
   * here. A prompt step has no conversation (§21.3): one whose row says `running` while no prompt
   * invocation of this tab holds it (a reload, or another tab's) is followed the same way.
   */
  private hasServerPendingStep(run: WorkflowRunView): boolean {
    if (run.status !== 'approved' && run.status !== 'stopped') return false;
    const live = this.handoffState.rowsFor(run.sessionId);
    return run.steps.some((step) => {
      if (step.removed) return false;
      const row = stepRow(step, live);
      if (!row || !SERVER_PENDING.has(row.status)) return false;
      if (isModelRunStep(step) || isModelStep(row)) {
        return row.status === 'running' && !this.queue.isRunning(run.runId, step.handoffId);
      }
      if (!row.targetSessionId) return false;
      return !this.chatState.isSessionLoading(row.targetSessionId);
    });
  }

  private reconcile(stale: ReadonlyMap<string, string>): void {
    for (const [runId, watch] of this.watches) {
      if (stale.has(runId)) continue;
      this.clearTimer(watch);
      this.watches.delete(runId);
    }
    for (const [runId, sessionId] of stale) {
      if (this.watches.has(runId)) continue;
      const watch: Watch = { sessionId, timer: null, delay: REFRESH_FIRST_MS };
      this.watches.set(runId, watch);
      this.schedule(runId, watch);
    }
  }

  private schedule(runId: string, watch: Watch): void {
    this.clearTimer(watch);
    if (this.hidden) return;
    watch.timer = setTimeout(() => void this.tick(runId, watch), watch.delay);
  }

  private async tick(runId: string, watch: Watch): Promise<void> {
    watch.timer = null;
    if (this.hidden || this.watches.get(runId) !== watch) return;
    let moved = 0;
    try {
      const run = await firstValueFrom(
        this.http.get<WorkflowRunView>(
          `${this.config.appApiUrl()}/sessions/${encodeURIComponent(watch.sessionId)}/workflow-runs/${encodeURIComponent(runId)}`,
        ),
      );
      moved = this.apply(run);
    } catch {
      // A failed read waits longer, like one that moved nothing; the run's row still says what it last knew.
    }
    if (this.watches.get(runId) !== watch) return;
    watch.delay = moved > 0 ? REFRESH_FIRST_MS : Math.min(watch.delay * 2, REFRESH_MAX_MS);
    this.schedule(runId, watch);
  }

  /** The run as read, into this tab's state, never moving a row backwards. Returns the rows moved. */
  private apply(run: WorkflowRunView): number {
    const current = this.runs.runsFor(run.sessionId).find((candidate) => candidate.runId === run.runId);
    // Keep what this tab knows that the read can't: a result it collected a moment ago.
    this.runs.upsertRun(current?.resultDeliveryId && !run.resultDeliveryId ? { ...run, resultDeliveryId: current.resultDeliveryId } : run);
    const rows = run.steps.flatMap((step) => (step.handoff ? [step.handoff] : []));
    return this.handoffState.advanceRows(rows, (row) => !!row.targetSessionId && this.chatState.isSessionLoading(row.targetSessionId));
  }

  private clearTimer(watch: Watch): void {
    if (watch.timer !== null) clearTimeout(watch.timer);
    watch.timer = null;
  }
}
