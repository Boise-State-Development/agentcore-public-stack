import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { ChatStateService } from '../chat/chat-state.service';
import { SessionMetadata } from '../models/session-metadata.model';
import { HandoffStateService } from './handoff-state.service';
import { RunProgress, runProgress, stepRow } from './workflow-run-progress';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { WorkflowRunView } from './workflow-run.models';

/** A step conversation's run: the source it came from and the run it is a step of. */
export interface RunLink {
  sourceSessionId: string;
  runId: string;
}

/**
 * What the sidebar needs to nest a workflow run's step conversations under their source
 * (docs/specs/workflow-runs.md §20.4, PR-W.4c).
 *
 * It reads the run's progress from the same sources as the run's row in the thread
 * (`WorkflowRunRowComponent`): the run as `WorkflowRunStateService` holds it, this tab's live
 * handoff rows and `runProgress`, with the same signals. So "2 of 4 finished" in the sidebar and
 * in the thread can never disagree. A run whose source this tab hasn't opened is read once with
 * `GET /sessions/{source}/workflow-runs/{runId}`, and again only when one of its step
 * conversations changes in the list; that read is the sidebar's, never on the turn path.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunSidebarService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly runState = inject(WorkflowRunStateService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly queue = inject(WorkflowRunQueueService);

  /** Runs read for the sidebar, by run id: only those whose source this tab hasn't loaded. */
  private readonly fetched = signal<ReadonlyMap<string, WorkflowRunView>>(new Map());
  /** The list state each fetched run was last read for, by run id, so a run is read once per change. */
  private readonly readFor = new Map<string, string>();
  private readonly inFlight = new Set<string>();

  /**
   * The run a conversation is a step of, or `null`. The list's `spawnedFrom.runId` survives a
   * reload; this tab's live handoff rows cover a step started a moment ago, before the list has
   * reloaded. Both name the same fact, written once, at the step's acceptance.
   */
  linkOf(session: SessionMetadata): RunLink | null {
    const spawned = session.spawnedFrom;
    if (spawned?.runId && spawned.sessionId) {
      return { sourceSessionId: spawned.sessionId, runId: spawned.runId };
    }
    const live = this.handoffState.allRows().find((row) => row.runId && row.targetSessionId === session.sessionId);
    return live?.runId ? { sourceSessionId: live.sessionId, runId: live.runId } : null;
  }

  /** The run, as the thread's row would read it: the source's own copy first, else the sidebar's. */
  runFor(link: RunLink): WorkflowRunView | null {
    const known = this.runState.runsFor(link.sourceSessionId).find((run) => run.runId === link.runId);
    return known ?? this.fetched().get(link.runId) ?? null;
  }

  /** `runProgress` with exactly the run row's signals. */
  progressFor(run: WorkflowRunView): RunProgress {
    return runProgress(run, this.handoffState.rowsFor(run.sessionId), {
      isLoading: (target) => this.chatState.isSessionLoading(target),
      isPending: (handoffId) => this.queue.isPending(run.runId, handoffId),
    });
  }

  /** Where a step conversation sits in its run's step order, or `-1` when the run doesn't say. */
  stepIndex(run: WorkflowRunView, sessionId: string): number {
    const liveRows = this.handoffState.rowsFor(run.sessionId);
    return run.steps.filter((step) => !step.removed).findIndex((step) => stepRow(step, liveRows)?.targetSessionId === sessionId);
  }

  /**
   * Read a run this tab doesn't hold, once per `listState` (what its step conversations look like
   * in the list). A run the source's own `GET /messages` replayed is never read here. A failed
   * read is left alone; the line then counts only the steps it lists.
   */
  ensure(link: RunLink, listState: string): void {
    if (this.runState.runsFor(link.sourceSessionId).some((run) => run.runId === link.runId)) return;
    if (this.readFor.get(link.runId) === listState || this.inFlight.has(link.runId)) return;
    this.readFor.set(link.runId, listState);
    this.inFlight.add(link.runId);
    const url = `${this.config.appApiUrl()}/sessions/${encodeURIComponent(link.sourceSessionId)}/workflow-runs/${encodeURIComponent(link.runId)}`;
    firstValueFrom(this.http.get<WorkflowRunView>(url))
      .then((run) => {
        if (run && run.runId === link.runId && Array.isArray(run.steps)) {
          this.fetched.update((map) => new Map(map).set(run.runId, run));
        }
      })
      .catch(() => undefined)
      .finally(() => this.inFlight.delete(link.runId));
  }
}
