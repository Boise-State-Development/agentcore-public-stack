import { ChangeDetectionStrategy, Component, computed, inject, output } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroInformationCircle } from '@ng-icons/heroicons/outline';

import { AgentIconComponent } from '../../agents/components/agent-icon.component';
import { ModelService } from '../../session/services/model/model.service';
import { estimateRunInputUsd, formatEstimateUsd } from '../../session/services/handoff/workflow-run-estimate';
import { WorkflowDesignerStore } from '../workflow-designer.store';
import { WorkflowFiles, WorkflowStepView } from '../workflow.models';

const FILES_LINE: Record<WorkflowFiles, string> = {
  none: 'Asks for no files',
  one: 'Asks for exactly one file',
  any: 'Takes any files, or none',
};

interface PreviewRow {
  step: WorkflowStepView;
  label: string;
  model: string;
  rate: string | null;
  emoji?: string;
  iconUrl?: string;
}

/**
 * The Preview tab is the sheet (docs/specs/workflow-designer.md §6.1, D3): what a person starting
 * this workflow will see, built by the server from the current draft with one
 * `PUT …?validate=true`. It writes nothing and makes no model call.
 *
 * Shown: the steps with their models, the files rule, the request, what blocks Start, and the
 * estimate's inputs. Start test run and Try in a chat are the starting half (PR-D.2's
 * `WorkflowStartService`), offered only for a saved workflow with no unsaved edits, because a run
 * reads the saved file.
 */
@Component({
  selector: 'app-workflow-preview-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentIconComponent],
  providers: [provideIcons({ heroExclamationTriangle, heroInformationCircle })],
  template: `
    @if (!store.ref()) {
      <p class="rounded-2xl border border-gray-200 bg-white px-4 py-3 text-sm/6 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
        Save the workflow once to preview the sheet a person starting it will see.
        @if (store.saveBlockedReason(); as reason) { {{ reason }} }
      </p>
    } @else if (!store.complete()) {
      <p class="rounded-2xl border border-gray-200 bg-white px-4 py-3 text-sm/6 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
        {{ store.saveBlockedReason() }} The preview needs an Agent on every step.
      </p>
    } @else if (document(); as doc) {
      <div class="space-y-4" [attr.aria-busy]="store.validating()">
        <p class="sr-only" role="status">{{ store.validating() ? 'Updating the preview' : '' }}</p>
        @if (store.previewError(); as error) {
          <p role="alert" class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm/6 text-red-800 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300">
            {{ error }}
          </p>
        }

        <section
          aria-labelledby="wf-sheet-title"
          class="rounded-2xl border border-gray-200 bg-white p-4 shadow-xs transition-opacity motion-reduce:transition-none dark:border-gray-700 dark:bg-gray-800"
          [class.opacity-60]="!store.previewCurrent()"
        >
          <h2 id="wf-sheet-title" class="text-sm/6 font-semibold text-gray-900 dark:text-white">{{ doc.workflow.title }}</h2>
          <p class="text-xs/5 text-gray-600 dark:text-gray-400">
            {{ rows().length }} {{ rows().length === 1 ? 'step' : 'steps' }} · {{ filesLine() }}
          </p>

          <div class="mt-3">
            <h3 class="text-xs/5 font-medium text-gray-700 dark:text-gray-300">Request</h3>
            @if (doc.workflow.request) {
              <p class="mt-0.5 whitespace-pre-line text-sm/6 text-gray-900 dark:text-white">{{ doc.workflow.request }}</p>
            } @else {
              <p class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">None by default: the starter writes one.</p>
            }
          </div>

          <ul role="list" class="mt-3 divide-y divide-gray-200 border-y border-gray-200 dark:divide-gray-700 dark:border-gray-700">
            @for (row of rows(); track row.step.name) {
              <li class="flex items-start gap-3 py-2">
                <app-agent-icon [agentId]="row.step.agentId" [iconUrl]="row.iconUrl" [emoji]="row.emoji" [size]="28" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ row.label }}</p>
                  @if (row.step.agent.canRun) {
                    <p class="truncate text-xs/5 text-gray-600 dark:text-gray-400">
                      {{ row.model }}@if (row.step.request) { · Own request }
                    </p>
                    @if (row.step.agent.changed) {
                      <p class="text-xs/5 text-gray-700 dark:text-gray-300">
                        This Agent has changed since it was added. A run uses its published version, now version {{ row.step.agent.publishedVersion }}.
                      </p>
                    }
                  } @else {
                    <p class="flex items-center gap-1 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                      <ng-icon name="heroExclamationTriangle" class="size-3.5 shrink-0" aria-hidden="true" />
                      You can’t use this Agent
                    </p>
                  }
                </div>
              </li>
            }
          </ul>
          <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400">
            A starter can change the request and remove a step on the sheet before they start.
          </p>

          @if (doc.sheet.blockers.length) {
            <div class="mt-3 rounded-2xl border border-state-warning-200 bg-state-warning-50 px-3 py-2 dark:border-state-warning-800/60 dark:bg-state-warning-900/20">
              <h3 class="flex items-center gap-1.5 text-xs/5 font-semibold text-state-warning-800 dark:text-state-warning-300">
                <ng-icon name="heroExclamationTriangle" class="size-4" aria-hidden="true" />
                Start is off until this is fixed
              </h3>
              <ul role="list" class="mt-1 space-y-0.5 text-xs/5 text-state-warning-800 dark:text-state-warning-200">
                @for (blocker of doc.sheet.blockers; track $index) {
                  <li>{{ blockerText(blocker.message, blocker.step) }}</li>
                }
              </ul>
            </div>
          } @else {
            <p class="mt-3 text-xs/5 text-gray-700 dark:text-gray-300">Ready to start: nothing blocks it.</p>
          }
        </section>

        <section aria-labelledby="wf-estimate-title" class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
          <h2 id="wf-estimate-title" class="text-sm/6 font-semibold text-gray-900 dark:text-white">Estimate</h2>
          <p class="text-xs/5 text-gray-600 dark:text-gray-400">
            The sheet prices each step’s first turn from these, plus the files the starter attaches.
          </p>
          <dl class="mt-2 space-y-1 text-xs/5">
            @for (row of rows(); track row.step.name) {
              <div class="flex justify-between gap-3">
                <dt class="min-w-0 truncate text-gray-700 dark:text-gray-300">{{ row.label }} · {{ row.model }}</dt>
                <dd class="shrink-0 tabular-nums text-gray-900 dark:text-white">{{ row.rate ?? 'No rate' }}</dd>
              </div>
            }
          </dl>
          <p class="mt-2 text-xs/5 text-gray-700 dark:text-gray-300">
            @if (floor(); as usd) {
              {{ usd }} for the request alone, before any file or follow-up.
            } @else {
              The estimate needs a rate for every step’s model; the sheet says “estimate unavailable” without one.
            }
          </p>
        </section>

        <p class="flex items-start gap-1.5 text-xs/5 text-gray-600 dark:text-gray-400">
          <ng-icon name="heroInformationCircle" class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          Building this preview makes no model call. Starting the workflow runs it for real: real conversations, on your quota.
        </p>
      </div>
    } @else if (store.previewError(); as error) {
      <p role="alert" class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm/6 text-red-800 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300">{{ error }}</p>
    } @else {
      <p class="text-sm/6 text-gray-600 dark:text-gray-400" role="status">Building the preview…</p>
    }
  `,
})
export class WorkflowPreviewTabComponent {
  protected readonly store = inject(WorkflowDesignerStore);
  private readonly models = inject(ModelService);

  /** Asks the page to start the saved workflow (`chat`: in a new conversation). */
  readonly start = output<'test' | 'chat'>();

  protected readonly document = this.store.document;

  protected readonly filesLine = computed(() => FILES_LINE[this.document()?.workflow.files ?? 'any']);

  protected readonly rows = computed<PreviewRow[]>(() => {
    const doc = this.document();
    if (!doc) return [];
    const mentions = new Map(this.store.agents().map((agent) => [agent.agentId, agent]));
    return doc.workflow.steps.map((step) => {
      const mention = mentions.get(step.agentId);
      const agent = step.agent;
      const model = agent.modelId ? (this.models.modelNameFor(agent.modelId) ?? agent.modelId) : 'The starter’s model';
      const rate = agent.inputPricePerMillionTokens;
      return {
        step,
        label: mention?.name || agent.name || 'An Agent you can’t use',
        model,
        rate: typeof rate === 'number' && rate > 0 ? `$${rate.toFixed(2)} / M input tokens` : null,
        emoji: mention?.emoji ?? agent.emoji,
        iconUrl: mention?.iconUrl,
      };
    });
  });

  /** The first turns' input for the request alone: a floor, since the files come at start. */
  protected readonly floor = computed(() => {
    const doc = this.document();
    if (!doc) return null;
    const usd = estimateRunInputUsd({
      steps: doc.workflow.steps.map((step) => ({
        modelId: step.agent.modelId ?? null,
        inputPricePerMillionTokens: step.agent.inputPricePerMillionTokens ?? null,
        request: step.effectiveRequest,
      })),
      card: '',
      fileTokens: [],
    });
    return usd === null ? null : formatEstimateUsd(usd);
  });

  protected blockerText(message: string, step?: string): string {
    if (!step) return message;
    const row = this.rows().find((r) => r.step.name === step);
    return `${row?.label ?? step}: ${message}`;
  }
}
