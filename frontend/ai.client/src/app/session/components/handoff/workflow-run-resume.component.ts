import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPauseCircle } from '@ng-icons/heroicons/outline';

import { WorkflowRunQueueService } from '../../services/handoff/workflow-run-queue.service';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { RunStepLaunch, WorkflowRunView } from '../../services/handoff/workflow-run.models';
import { WorkflowRunService, agentsLabel } from '../../services/handoff/workflow-run.service';

interface UnstartedRun {
  run: WorkflowRunView;
  steps: RunStepLaunch[];
  /** False when this browser has no saved envelope for the run, so the card as reviewed is unknown. */
  startable: boolean;
}

/**
 * An approved run's steps that never started, after a reload (docs/specs/workflow-runs.md §6.1,
 * §19 PR-W.4a). The queue lives as long as the tab, so a reload with steps still queued leaves
 * them `open` with no conversation. They read "Not started" here, with one Start that resumes the
 * queue with the envelope as approved.
 *
 * The handoff row's look, above the composer. W.4b's run row in the thread takes this over.
 */
@Component({
  selector: 'app-workflow-run-resume',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroPauseCircle })],
  template: `
    @for (entry of unstarted(); track entry.run.runId) {
      <section
        [attr.aria-labelledby]="headingId(entry.run.runId)"
        class="mb-2 rounded-2xl border border-gray-200 bg-white px-4 py-3 dark:border-gray-700 dark:bg-gray-900"
      >
        <h2 [id]="headingId(entry.run.runId)" class="text-sm/6 font-medium text-gray-900 dark:text-white">
          {{ label(entry.steps.length) }} not started
        </h2>
        <ul role="list" class="mt-1 space-y-0.5">
          @for (step of entry.steps; track step.handoffId) {
            <li class="flex items-center gap-2 text-sm/6 text-gray-700 dark:text-gray-300">
              <ng-icon name="heroPauseCircle" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
              <span class="min-w-0 truncate">{{ step.agentName }} · Not started</span>
            </li>
          }
        </ul>
        @if (entry.startable) {
          <button
            type="button"
            class="mt-2 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
            (click)="start(entry.run)"
          >
            Start {{ label(entry.steps.length) }}
          </button>
        } @else {
          <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
            These were approved in another browser, so they can be started only from there.
          </p>
        }
      </section>
    }
  `,
})
export class WorkflowRunResumeComponent {
  private readonly state = inject(WorkflowRunStateService);
  private readonly queue = inject(WorkflowRunQueueService);
  private readonly runs = inject(WorkflowRunService);

  /** The source conversation: the one being viewed. */
  readonly sessionId = input.required<string>();

  protected readonly unstarted = computed<UnstartedRun[]>(() =>
    this.state.runsFor(this.sessionId()).flatMap((run) => {
      const steps = this.queue.unstartedSteps(run);
      if (steps.length === 0) return [];
      return [{ run, steps, startable: this.queue.storedEnvelope(run.runId) !== null }];
    }),
  );

  protected headingId(runId: string): string {
    return `run-resume-${runId}`;
  }

  protected label(count: number): string {
    return agentsLabel(count);
  }

  protected start(run: WorkflowRunView): void {
    this.runs.resume(run);
  }
}
