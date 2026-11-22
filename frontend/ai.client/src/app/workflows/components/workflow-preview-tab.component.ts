import { ChangeDetectionStrategy, Component, computed, inject, output, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroInformationCircle } from '@ng-icons/heroicons/outline';

import { AgentIconComponent } from '../../agents/components/agent-icon.component';
import { stepNameLabel } from '../../session/services/handoff/workflow-run-progress';
import { PromptStepIconComponent } from './prompt-step-icon.component';
import { modelReasonCopy } from './workflow-step-tab.component';
import { ModelService } from '../../session/services/model/model.service';
import { WorkflowStartService } from '../../services/workflows/workflow-start.service';
import { WorkflowTestRunService } from '../workflow-test-run.service';
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
  /** A prompt step (§21.3): named by its step, with its model in place of an Agent. */
  prompt: boolean;
  /** Why a prompt step's model can't be used, when it can't. */
  unavailable: string;
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
 * reads the saved file. Both open a new conversation with the sheet up, so nothing runs until the
 * author presses Start there; the run drawn on the canvas is D.4.
 */
@Component({
  selector: 'app-workflow-preview-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentIconComponent, PromptStepIconComponent],
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
          <p role="alert" class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-800/60 dark:bg-state-danger-900/20 dark:text-state-danger-300">
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
                @if (row.prompt) {
                  <app-prompt-step-icon [size]="28" />
                } @else {
                  <app-agent-icon [agentId]="row.step.agentId" [iconUrl]="row.iconUrl" [emoji]="row.emoji" [size]="28" />
                }
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ row.label }}</p>
                  @if (row.step.agent.canRun) {
                    <p class="truncate text-xs/5 text-gray-600 dark:text-gray-400">
                      {{ row.model }}@if (row.step.request) { · Own request }
                    </p>
                    @if (row.step.agent.changed && !row.prompt) {
                      <p class="text-xs/5 text-gray-700 dark:text-gray-300">
                        This Agent has changed since it was added. A run uses its published version, now version {{ row.step.agent.publishedVersion }}.
                      </p>
                    }
                  } @else {
                    <p class="flex items-center gap-1 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                      <ng-icon name="heroExclamationTriangle" class="size-3.5 shrink-0" aria-hidden="true" />
                      {{ row.prompt ? row.unavailable : 'You can’t use this Agent' }}
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

        <section aria-labelledby="wf-start-title" class="space-y-2">
          <h2 id="wf-start-title" class="sr-only">Start</h2>
          <div class="flex flex-wrap items-center gap-2">
            <button
              type="button"
              (click)="startTestRun()"
              [disabled]="!canStart()"
              [attr.aria-describedby]="startBlockedReason() ? 'wf-start-blocked' : null"
              class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white shadow-xs transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:focus-visible:outline-primary-accessible-dark"
            >
              {{ starting() === 'test' ? 'Opening…' : 'Start test run' }}
            </button>
            <button
              type="button"
              (click)="tryInChat()"
              [disabled]="!canStart()"
              [attr.aria-describedby]="startBlockedReason() ? 'wf-start-blocked' : null"
              class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus-visible:outline-primary-accessible-dark"
            >
              {{ starting() === 'chat' ? 'Opening…' : 'Try in a chat' }}
            </button>
          </div>
          @if (startBlockedReason(); as reason) {
            <p id="wf-start-blocked" class="text-xs/5 text-gray-700 dark:text-gray-300">{{ reason }}</p>
          } @else {
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">
              Start test run opens this sheet here, in a new conversation titled “Test: {{ doc.workflow.title }}”, and draws the run on the canvas.
              Try in a chat opens it in a new conversation and takes you there. Nothing runs until you press Start on the sheet.
            </p>
          }
        </section>

        <p class="flex items-start gap-1.5 text-xs/5 text-gray-600 dark:text-gray-400">
          <ng-icon name="heroInformationCircle" class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          Building this preview makes no model call. Starting the workflow runs it for real: real conversations, on your quota.
        </p>
      </div>
    } @else if (store.previewError(); as error) {
      <p role="alert" class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-800/60 dark:bg-state-danger-900/20 dark:text-state-danger-300">{{ error }}</p>
    } @else {
      <p class="text-sm/6 text-gray-600 dark:text-gray-400" role="status">Building the preview…</p>
    }
  `,
})
export class WorkflowPreviewTabComponent {
  protected readonly store = inject(WorkflowDesignerStore);
  private readonly models = inject(ModelService);
  private readonly starter = inject(WorkflowStartService);
  private readonly testRuns = inject(WorkflowTestRunService);

  /** The test run's sheet is up in the panel: the designer switches to its Test run tab. */
  readonly testRunOpened = output<void>();

  protected readonly starting = signal<'test' | 'chat' | null>(null);

  /** Why Start is off, or null. A run reads the saved file, so unsaved edits come first. */
  protected readonly startBlockedReason = computed(() => {
    if (this.store.dirty()) return 'Save your changes to start: a run uses the saved workflow.';
    const doc = this.document();
    if (doc && !doc.sheet.startable) return 'Fix what the sheet lists above to start it.';
    return null;
  });

  protected readonly canStart = computed(() => !!this.store.ref() && !this.starting() && !this.startBlockedReason());

  /** Open the sheet in this page's panel, in a new "Test: …" conversation, and stay here (PR-D.4). */
  protected async startTestRun(): Promise<void> {
    const ref = this.store.ref();
    if (!ref || !this.canStart()) return;
    this.starting.set('test');
    try {
      if (await this.testRuns.begin(ref)) this.testRunOpened.emit();
    } finally {
      this.starting.set(null);
    }
  }

  /** Open a new conversation with this workflow's sheet up, and go there (PR-D.2's start path). */
  protected async tryInChat(): Promise<void> {
    const ref = this.store.ref();
    if (!ref || !this.canStart()) return;
    this.starting.set('chat');
    try {
      await this.starter.start(ref);
    } finally {
      this.starting.set(null);
    }
  }

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
      const prompt = step.kind === 'prompt';
      return {
        step,
        prompt,
        // A prompt step is called by its own name; several can share a model (§21.3).
        label: prompt ? stepNameLabel(step.name) : mention?.name || agent.name || 'An Agent you can’t use',
        model: prompt ? `Prompt · ${step.model?.name || model}` : model,
        unavailable: prompt ? modelReasonCopy(step.model?.reason) : '',
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
        ...(step.kind === 'prompt' ? { card: false, files: step.reads.includes('files') } : {}),
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
