import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, effect, inject, signal, untracked } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { DeliveryStateService } from './delivery-state.service';
import { DeliveryView } from './handoff.models';
import { HandoffNotificationService } from './handoff-notification.service';
import { HandoffStateService } from './handoff-state.service';
import { WorkflowRunMergeService, mergeInstructionOf } from './workflow-run-merge.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { WorkflowRunView } from './workflow-run.models';
import { runProgress } from './workflow-run-progress';

/** `POST …/workflow-runs/{runId}/collect` (PR-W.2). */
export interface CollectWorkflowRunResponse {
  deliveryId: string;
  /** False when an earlier collect made this card. */
  created: boolean;
  /** The card's own status: `pending` until a send carries it. */
  status: string;
  delivery?: DeliveryView;
}

interface WatchedRun {
  runId: string;
  sourceSessionId: string;
}

/**
 * A workflow run's one result (docs/specs/workflow-runs.md §7, §20.3, PR-W.4b).
 *
 * - **Collect** (`…/collect`) makes the run's one card, with a part per kept step, and puts it on
 *   the source's composer through `DeliveryStateService`, where it takes one of the three per-send
 *   slots and is attached by default (the user started the run). A collect that answers
 *   `created: false` hands back the card already made; it is put back only while it still waits.
 * - **The watch.** A run started or resumed in this tab is watched until every kept step is done or
 *   stopped. Then, unless the user pressed Stop run, it says "*N* Agents finished · Open" (Open
 *   scrolls to the run's row) and collects. A run nobody watched here (a reload) collects only on
 *   the row's Bring back results.
 * - **The merge (PR-D.5b).** When the watch's collect made the card (`created: true`, the run's
 *   one `claim_result`) and the saved workflow's Result merges, `WorkflowRunMergeService` sends
 *   the one merge turn if the source is on screen, idle and its composer empty, checked then.
 *   Otherwise the card waits, as in step 1, and the row offers Merge now.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunResultService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly runs = inject(WorkflowRunStateService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly queue = inject(WorkflowRunQueueService);
  private readonly deliveryState = inject(DeliveryStateService);
  private readonly notifications = inject(HandoffNotificationService);
  private readonly toast = inject(ToastService);
  private readonly merge = inject(WorkflowRunMergeService);

  private readonly watched = signal<ReadonlyMap<string, WatchedRun>>(new Map());
  private readonly collecting = signal<ReadonlySet<string>>(new Set());
  /** Run id → the result card this tab put on the composer. */
  private readonly collected = signal<ReadonlyMap<string, string>>(new Map());

  constructor() {
    effect(() => {
      for (const watch of this.watched().values()) {
        const run = this.runs.runsFor(watch.sourceSessionId).find((r) => r.runId === watch.runId);
        if (!run) continue;
        if (run.status !== 'approved') {
          // Stopped (here or elsewhere): the row offers Bring back results; nothing is automatic.
          untracked(() => this.unwatch(run.runId));
          continue;
        }
        const progress = runProgress(run, this.handoffState.rowsFor(run.sessionId), {
          isLoading: (target) => this.chatState.isSessionLoading(target),
          isPending: (handoffId) => this.queue.isPending(run.runId, handoffId),
        });
        if (!progress.settled) continue;
        untracked(() => {
          this.unwatch(run.runId);
          this.onSettled(run, progress.done);
        });
      }
    });
  }

  /** Watch a run this tab started or resumed, so its result comes back when its last step settles. */
  watch(run: Pick<WorkflowRunView, 'runId' | 'sessionId'>): void {
    this.watched.update((map) => new Map(map).set(run.runId, { runId: run.runId, sourceSessionId: run.sessionId }));
  }

  /** A collect for this run is in flight. */
  isCollecting(runId: string): boolean {
    return this.collecting().has(runId);
  }

  /** The run's result card: the one this tab collected, else the run row's. */
  resultCardOf(run: Pick<WorkflowRunView, 'runId' | 'resultDeliveryId'>): string | undefined {
    return this.collected().get(run.runId) ?? run.resultDeliveryId;
  }

  /** The run's result card is waiting on its source's composer. */
  isOnComposer(run: Pick<WorkflowRunView, 'runId' | 'sessionId' | 'resultDeliveryId'>): boolean {
    const deliveryId = this.resultCardOf(run);
    if (!deliveryId) return false;
    return this.deliveryState.cardsFor(run.sessionId).some((card) => card.deliveryId === deliveryId);
  }

  /**
   * Bring the run's results back as one card on the source's composer. `quiet` is the watch's own
   * collect, which says nothing when it fails: the row still offers Bring back results.
   */
  async collect(
    run: Pick<WorkflowRunView, 'runId' | 'sessionId'>,
    options: { quiet?: boolean; merge?: boolean } = {},
  ): Promise<void> {
    if (this.isCollecting(run.runId)) return;
    this.collecting.update((set) => new Set(set).add(run.runId));
    try {
      const result = await firstValueFrom(
        this.http.post<CollectWorkflowRunResponse>(
          `${this.config.appApiUrl()}/sessions/${encodeURIComponent(run.sessionId)}/workflow-runs/${encodeURIComponent(run.runId)}/collect`,
          {},
        ),
      );
      this.recordResult(run, result.deliveryId);
      if (result.status === 'pending' && result.delivery) {
        this.deliveryState.add(run.sessionId, result.delivery);
        this.collected.update((map) => new Map(map).set(run.runId, result.deliveryId));
        // Only the collect that made the card may merge by itself: that is the run's one claim.
        if (options.merge && result.created && this.mergeIfReady(run, result.deliveryId)) return;
        if (!options.quiet) this.toast.success('Results added to your next message');
        return;
      }
      if (!options.quiet) {
        this.toast.info(result.status === 'attached' ? 'These results were already sent' : 'These results were removed');
      }
    } catch (error) {
      if (options.quiet) return;
      this.toast.error('The results couldn’t be brought back', describe(error));
    } finally {
      this.collecting.update((set) => {
        const next = new Set(set);
        next.delete(run.runId);
        return next;
      });
    }
  }

  private onSettled(run: WorkflowRunView, done: number): void {
    if (done === 0) return;
    const first = run.steps.find((step) => !step.removed);
    const label = `${done} ${done === 1 ? 'Agent' : 'Agents'} finished`;
    this.toast.success(label, undefined, {
      action: first
        ? { label: 'Open', handler: () => this.notifications.openSource({ handoffId: first.handoffId, sourceSessionId: run.sessionId }) }
        : undefined,
    });
    if (!run.resultDeliveryId) void this.collect(run, { quiet: true, merge: true });
  }

  /** The merge turn, when the run's Result merges and nothing is in the way now (PR-D.5b). */
  private mergeIfReady(run: Pick<WorkflowRunView, 'runId' | 'sessionId'>, deliveryId: string): boolean {
    const current = this.runs.runsFor(run.sessionId).find((r) => r.runId === run.runId);
    if (!current || mergeInstructionOf(current) === null) return false;
    return this.merge.startIfReady(current, deliveryId);
  }

  /** Keep the run's `resultDeliveryId` in step with the server, so the row knows a result exists. */
  private recordResult(run: Pick<WorkflowRunView, 'runId' | 'sessionId'>, deliveryId: string): void {
    const current = this.runs.runsFor(run.sessionId).find((r) => r.runId === run.runId);
    if (current && current.resultDeliveryId !== deliveryId) this.runs.upsertRun({ ...current, resultDeliveryId: deliveryId });
  }

  private unwatch(runId: string): void {
    this.watched.update((map) => {
      if (!map.has(runId)) return map;
      const next = new Map(map);
      next.delete(runId);
      return next;
    });
  }
}

function describe(error: unknown): string {
  if (error instanceof HttpErrorResponse) {
    if (error.status === 422) return 'No Agent has a result to bring back yet.';
    if (error.status === 404) return 'The conversation can’t be found.';
  }
  return 'Something went wrong. Try again.';
}
