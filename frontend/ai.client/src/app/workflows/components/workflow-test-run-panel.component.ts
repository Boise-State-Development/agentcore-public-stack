import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowTopRightOnSquare, heroInformationCircle } from '@ng-icons/heroicons/outline';

import { WorkflowRef } from '../../services/workflows/workflow.models';
import { DeliveryCardsComponent } from '../../session/components/handoff/delivery-cards.component';
import { WorkflowRunRowComponent } from '../../session/components/handoff/workflow-run-row.component';
import { WorkflowRunSheetComponent } from '../../session/components/handoff/workflow-run-sheet.component';
import { DeliveryStateService } from '../../session/services/handoff/delivery-state.service';
import { WorkflowRunStateService } from '../../session/services/handoff/workflow-run-state.service';
import { SessionService } from '../../session/services/session/session.service';
import { WorkflowTestRunService, isStartedRun } from '../workflow-test-run.service';

type PanelPhase = 'sheet' | 'run' | 'loading' | 'failed' | 'none';

/**
 * The designer's Test run tab (docs/specs/workflow-designer.md §6.2, PR-D.4): the test run's sheet,
 * then its run.
 *
 * - **The sheet** is the starter's own `WorkflowRunSheetComponent`, unchanged, hosted here for the
 *   test conversation (it reads `reviewFor(sessionId)`, which is not tied to a page). A note above
 *   it says the run spends quota and that everything it makes can be opened afterwards.
 * - **The run**, once Start is pressed: the run's own row (`WorkflowRunRowComponent`), so each
 *   step's relayed prompt is answered here exactly as under the row in a thread, and each step
 *   line opens its conversation. Then the result card (`DeliveryCardsComponent`), which waits on
 *   the test conversation's composer, with a link to that conversation.
 *
 * A run this tab doesn't hold (after a reload) is loaded by id; anything that shows it was never
 * started (the sheet was cancelled, or closed by the reload before Start) forgets the test run.
 */
@Component({
  selector: 'app-workflow-test-run-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, WorkflowRunSheetComponent, WorkflowRunRowComponent, DeliveryCardsComponent],
  providers: [provideIcons({ heroArrowTopRightOnSquare, heroInformationCircle })],
  template: `
    @if (record(); as rec) {
      <div class="space-y-3">
        @switch (phase()) {
          @case ('sheet') {
            <p class="flex items-start gap-1.5 rounded-2xl bg-white px-3 py-2 text-xs/5 text-gray-700 ring-1 ring-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:ring-gray-700">
              <ng-icon name="heroInformationCircle" class="mt-0.5 size-4 shrink-0 text-gray-600 dark:text-gray-400" aria-hidden="true" />
              <span>
                A test run is a real run: each Agent works in its own conversation, on your quota. Everything it makes
                can be opened afterwards, from here or from “{{ sourceTitle() }}” in your conversations.
              </span>
            </p>
            <app-workflow-run-sheet [sessionId]="rec.sessionId" />
          }
          @case ('run') {
            @if (run(); as r) {
              <app-workflow-run-row [run]="r" [offerSaveAsWorkflow]="false" />
              @if (hasResultCard()) {
                <div>
                  <app-delivery-cards [sessionId]="rec.sessionId" />
                  <p class="px-1 text-xs/5 text-gray-600 dark:text-gray-400">
                    The result waits on the test conversation’s message box. Open it there to send or remove it.
                  </p>
                </div>
              }
            }
          }
          @case ('loading') {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400" role="status">Loading the test run…</p>
          }
          @case ('failed') {
            <div role="alert" class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-800/60 dark:bg-state-danger-900/20 dark:text-state-danger-300">
              The test run couldn’t be loaded.
              <button
                type="button"
                (click)="reload()"
                class="ml-1 rounded-md font-medium underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark"
              >
                Try again
              </button>
            </div>
          }
        }

        @if (phase() !== 'sheet') {
          <div class="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-xs/5">
            <a
              [routerLink]="['/s', rec.sessionId]"
              class="inline-flex items-center gap-1 rounded-md font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:focus-visible:outline-primary-accessible-dark"
            >
              Open the test conversation
              <ng-icon name="heroArrowTopRightOnSquare" class="size-3.5" aria-hidden="true" />
            </a>
            @if (canClose()) {
              <button
                type="button"
                (click)="close()"
                class="rounded-md font-medium text-gray-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:focus-visible:outline-primary-accessible-dark"
              >
                Close test run
              </button>
            }
          </div>
          @if (canClose()) {
            <p class="px-1 text-xs/5 text-gray-600 dark:text-gray-400">Closing it here keeps its conversations.</p>
          }
        }
      </div>
    }
  `,
})
export class WorkflowTestRunPanelComponent {
  private readonly testRuns = inject(WorkflowTestRunService);
  private readonly runState = inject(WorkflowRunStateService);
  private readonly deliveries = inject(DeliveryStateService);
  private readonly sessions = inject(SessionService);

  readonly ref = input.required<WorkflowRef>();

  protected readonly record = computed(() => this.testRuns.recordFor(this.ref()));
  private readonly review = computed(() => this.runState.reviewFor(this.record()?.sessionId));
  protected readonly run = computed(() => this.testRuns.runFor(this.ref()));

  private readonly loadState = signal<'idle' | 'loading' | 'failed'>('idle');
  /** The run this panel last asked for, so a failed load is retried only on Try again. */
  private requested: string | null = null;

  protected readonly phase = computed<PanelPhase>(() => {
    if (!this.record()) return 'none';
    if (this.review()) return 'sheet';
    if (isStartedRun(this.run())) return 'run';
    return this.loadState() === 'failed' ? 'failed' : 'loading';
  });

  protected readonly sourceTitle = computed(() => {
    const sessionId = this.record()?.sessionId;
    const listed = this.sessions.mergedSessionsResource().sessions.find((s) => s.sessionId === sessionId);
    return listed?.title?.trim() || 'Test';
  });

  protected readonly hasResultCard = computed(() => {
    const record = this.record();
    return !!record && this.deliveries.cardsFor(record.sessionId).length > 0;
  });

  /** Offered once nothing is left running, so a run going on can't be dropped from view by accident. */
  protected readonly canClose = computed(() => {
    const phase = this.phase();
    if (phase === 'failed') return true;
    const run = this.run();
    if (phase !== 'run' || !run) return false;
    return run.status === 'stopped' || this.testRuns.progress(run).settled;
  });

  constructor() {
    // The sheet created the run: remember its id for a reload or a later visit.
    effect(() => {
      const runId = this.review()?.created?.run.runId;
      if (runId) untracked(() => this.testRuns.setRunId(this.ref(), runId));
    });

    // No sheet and no started run here: load it by id, or forget a test run that never started.
    effect(() => {
      if (this.phase() !== 'loading') return;
      const record = this.record();
      untracked(() => {
        if (!record) return;
        if (!record.runId) {
          this.testRuns.forget(this.ref());
          return;
        }
        if (this.requested === record.runId) return;
        this.requested = record.runId;
        void this.load({ sessionId: record.sessionId, runId: record.runId });
      });
    });
  }

  protected reload(): void {
    const record = this.record();
    if (!record?.runId) return;
    void this.load({ sessionId: record.sessionId, runId: record.runId });
  }

  protected close(): void {
    this.testRuns.forget(this.ref());
  }

  private async load(record: { sessionId: string; runId: string }): Promise<void> {
    this.loadState.set('loading');
    const outcome = await this.testRuns.load(record);
    if (outcome === 'gone') {
      this.loadState.set('idle');
      this.testRuns.forget(this.ref());
      return;
    }
    this.loadState.set(outcome === 'failed' ? 'failed' : 'idle');
  }
}
