import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { AgentHandoffService } from './agent-handoff.service';
import { HandoffStatus } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';
import { DEFAULT_RUN_CONCURRENCY, RunEnvelope, RunStepLaunch, WorkflowRunView } from './workflow-run.models';

/** A step stops holding a slot once its row reads one of these (spec §19 PR-W.4a). */
const SETTLED: ReadonlySet<HandoffStatus> = new Set(['done', 'needs_input', 'stopped', 'dismissed', 'abandoned']);

/** Local-storage key prefix for an approved run's envelope, kept until every step has started. */
const STORAGE_PREFIX = 'aps.workflowRun.';
/** An approved run's unsent steps stay startable for as long as their rows do (`ABANDON_AFTER`). */
const STORAGE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

interface QueuedRun {
  envelope: RunEnvelope;
  /** Steps not started yet, in step order. */
  pending: readonly RunStepLaunch[];
  /** Handoff ids of the steps holding a slot. */
  running: readonly string[];
}

interface StoredEnvelope {
  envelope: RunEnvelope;
  savedAt: number;
}

/**
 * The tab's queue for approved workflow runs (docs/specs/workflow-runs.md §6.1, PR-W.4a).
 *
 * At most `limits.concurrency` steps of a run start at once, each through the single handoff's
 * own background send (`AgentHandoffService.startRunStep`: no navigation, the user stays on the
 * source). The next starts when a running one settles: its row reaches `done`, `needs_input` or
 * `stopped`, or its stream ends without the row moving (a refused or undelivered send).
 *
 * Keyed by run id and provided in root, so it outlives the sheet and every route change; W.4b's
 * run row and W.4c's sidebar read it. A run lives as long as the tab (§6.1), so the page-unload
 * guard asks before closing a tab with queued or running steps.
 *
 * Every step is sent with the run's envelope as approved: exactly its `fileIds`, and the card as
 * reviewed. The envelope is also written to local storage until the last step has started, so a
 * reload can start the steps it never started with the same card (the run row stores the files,
 * not the card).
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunQueueService {
  private readonly handoff = inject(AgentHandoffService);
  private readonly handoffState = inject(HandoffStateService);

  private readonly runs = signal<ReadonlyMap<string, QueuedRun>>(new Map());

  /** True while any run in this tab has a step queued or running. */
  readonly hasActiveRun = computed(() => [...this.runs().values()].some((run) => run.pending.length + run.running.length > 0));

  constructor() {
    effect(() => {
      const runs = this.runs();
      for (const [runId, run] of runs) {
        if (run.running.length === 0) continue;
        const rows = this.handoffState.rowsFor(run.envelope.sourceSessionId);
        for (const handoffId of run.running) {
          const row = rows.find((candidate) => candidate.handoffId === handoffId);
          if (row && SETTLED.has(row.status)) untracked(() => this.settle(runId, handoffId));
        }
      }
    });

    if (typeof window !== 'undefined') {
      const guard = (event: BeforeUnloadEvent) => {
        if (!this.hasActiveRun()) return;
        event.preventDefault();
        // Older browsers need a returnValue to show the prompt.
        event.returnValue = '';
      };
      window.addEventListener('beforeunload', guard);
      inject(DestroyRef).onDestroy(() => window.removeEventListener('beforeunload', guard));
    }
  }

  /** True while this run has steps in the tab's queue (queued or running). */
  isQueued(runId: string): boolean {
    return this.runs().has(runId);
  }

  /** Steps of this run still waiting for a slot. */
  pendingCount(runId: string): number {
    return this.runs().get(runId)?.pending.length ?? 0;
  }

  /**
   * Queue an approved run's steps and start the first `concurrency` of them. A step already
   * queued or running for this run is not queued twice.
   */
  enqueue(envelope: RunEnvelope, steps: readonly RunStepLaunch[]): void {
    const current = this.runs().get(envelope.runId);
    const known = new Set([...(current?.pending ?? []).map((s) => s.handoffId), ...(current?.running ?? [])]);
    const fresh = steps.filter((step) => !known.has(step.handoffId));
    if (!current && fresh.length === 0) return;
    this.setRun(envelope.runId, {
      envelope,
      pending: [...(current?.pending ?? []), ...fresh],
      running: current?.running ?? [],
    });
    this.pump(envelope.runId);
  }

  /** The envelope this browser saved for an approved run, if it has one and it is still fresh. */
  storedEnvelope(runId: string): RunEnvelope | null {
    const stored = readStored(runId);
    return stored?.envelope ?? null;
  }

  /**
   * The steps of an approved run that were never started (spec §19 PR-W.4a "Not started"): open
   * rows with no target conversation, not removed, and not in this tab's queue.
   */
  unstartedSteps(run: WorkflowRunView): RunStepLaunch[] {
    if (run.status !== 'approved') return [];
    const queued = this.runs().get(run.runId);
    const held = new Set([...(queued?.pending ?? []).map((s) => s.handoffId), ...(queued?.running ?? [])]);
    const live = this.handoffState.rowsFor(run.sessionId);
    return run.steps
      .filter((step) => !step.removed && !held.has(step.handoffId))
      .filter((step) => {
        const row = live.find((candidate) => candidate.handoffId === step.handoffId) ?? step.handoff;
        return !!row && row.status === 'open' && !row.targetSessionId;
      })
      .map((step) => ({
        stepId: step.stepId,
        handoffId: step.handoffId,
        agentId: step.agentId,
        agentName: step.agentName,
        request: step.request,
      }));
  }

  private pump(runId: string): void {
    const run = this.runs().get(runId);
    if (!run) return;
    const limit = Math.max(1, run.envelope.concurrency || DEFAULT_RUN_CONCURRENCY);
    const free = limit - run.running.length;
    if (free <= 0 || run.pending.length === 0) {
      if (run.pending.length === 0 && run.running.length === 0) this.finish(runId);
      return;
    }
    const starting = run.pending.slice(0, free);
    this.setRun(runId, {
      ...run,
      pending: run.pending.slice(starting.length),
      running: [...run.running, ...starting.map((step) => step.handoffId)],
    });
    for (const step of starting) this.start(run.envelope, step);
  }

  private start(envelope: RunEnvelope, step: RunStepLaunch): void {
    let sent: Promise<void>;
    try {
      sent = this.handoff.startRunStep({
        sourceSessionId: envelope.sourceSessionId,
        handoffId: step.handoffId,
        agentId: step.agentId,
        agentName: step.agentName,
        request: step.request,
        sourceMessageIndex: envelope.sourceMessageIndex,
        excerptTokens: envelope.excerptTokens,
        turnsCovered: envelope.turnsCovered,
        sourceTitle: envelope.sourceTitle,
        contextText: envelope.contextText,
        // Exactly the run's approved files, on every step: nothing on the server checks a step's
        // `fileIds` against the run's list in step 1 (spec §19 PR-W.1, "For W.4a").
        sharedFileIds: [...envelope.fileIds],
        filesUnticked: envelope.filesUnticked,
        fileUploadIds: envelope.fileUploadIds,
        invokedSkillIds: envelope.invokedSkillIds,
        folders: envelope.folders,
        runId: envelope.runId,
        stepId: step.stepId,
      });
    } catch (error) {
      console.error('Workflow run step failed to start:', error);
      this.settle(envelope.runId, step.handoffId);
      return;
    }
    // The stream ended (or never began): the slot is free whatever the row says next.
    sent
      .catch((error) => console.error('Workflow run step send failed:', error))
      .finally(() => this.settle(envelope.runId, step.handoffId));
  }

  /** A step stopped holding its slot. Idempotent: a row settling and its stream ending both land here. */
  private settle(runId: string, handoffId: string): void {
    const run = this.runs().get(runId);
    if (!run || !run.running.includes(handoffId)) return;
    this.setRun(runId, { ...run, running: run.running.filter((id) => id !== handoffId) });
    this.pump(runId);
  }

  private finish(runId: string): void {
    this.runs.update((map) => {
      if (!map.has(runId)) return map;
      const next = new Map(map);
      next.delete(runId);
      return next;
    });
  }

  private setRun(runId: string, run: QueuedRun): void {
    this.runs.update((map) => new Map(map).set(runId, run));
    if (run.pending.length > 0) writeStored(run.envelope);
    else removeStored(runId);
  }
}

function storageKey(runId: string): string {
  return `${STORAGE_PREFIX}${runId}`;
}

function readStored(runId: string): StoredEnvelope | null {
  try {
    const raw = localStorage.getItem(storageKey(runId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredEnvelope;
    if (!parsed?.envelope || parsed.envelope.runId !== runId || !Array.isArray(parsed.envelope.fileIds)) return null;
    if (typeof parsed.savedAt !== 'number' || Date.now() - parsed.savedAt > STORAGE_MAX_AGE_MS) {
      localStorage.removeItem(storageKey(runId));
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function writeStored(envelope: RunEnvelope): void {
  try {
    const stored: StoredEnvelope = { envelope, savedAt: readStored(envelope.runId)?.savedAt ?? Date.now() };
    localStorage.setItem(storageKey(envelope.runId), JSON.stringify(stored));
  } catch {
    // Private mode or a full quota: the run still starts; only a reload loses the card.
  }
}

function removeStored(runId: string): void {
  try {
    localStorage.removeItem(storageKey(runId));
  } catch {
    // Nothing to clean up.
  }
}
