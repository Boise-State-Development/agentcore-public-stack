import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowRight, heroChevronRight, heroMinusCircle, heroPaperClip, heroXMark } from '@ng-icons/heroicons/outline';

import { AgentIconComponent } from '../../../agents/components/agent-icon.component';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { formatFileSize } from '../../services/handoff/handoff-prompt';
import { MAX_SHARED_FILES } from '../../services/handoff/handoff.models';
import { estimateRunInputUsd, formatEstimateUsd } from '../../services/handoff/workflow-run-estimate';
import { RunReviewStep, RunShareableFile } from '../../services/handoff/workflow-run.models';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { WorkflowRunService, agentsLabel, canStart, keptSteps, readersOf, removalOf, stepRequest } from '../../services/handoff/workflow-run.service';
import { ModelService } from '../../services/model/model.service';
import { WorkflowGraphComponent } from '../../../workflows/components/workflow-graph.component';
import { MAX_WORKFLOW_STEPS } from '../../../workflows/workflow.models';
import { reviewGraph } from '../../services/handoff/workflow-run-graph';

/** One step as the sheet renders it. */
interface StepRow {
  step: RunReviewStep;
  /** The Agent's name, or for a saved step the starter can't use, a stand-in naming the step. */
  label: string;
  modelName: string;
  emoji?: string;
  iconUrl?: string;
  ownRequest: boolean;
  /** A saved workflow's step whose Agent the starter can't use (§11). */
  unavailable: boolean;
  /** The saved step's name, for Mark as reviewed. */
  stepName?: string;
  /** "This Agent has changed since it was added…" (§10), when it has. */
  changedLine?: string;
  /** The kept steps that read this one, which Remove takes with it (PR-D.5a), as the sheet names them. */
  readers: string[];
}

/**
 * The run sheet: several `@`-mentioned Agents reviewed once (docs/specs/workflow-runs.md §4, §20.1).
 *
 * The single review sheet's look and controls, widened (`HandoffReviewSheetComponent` is left as it
 * is): a labelled region above the composer, never a modal, so the thread the card comes from
 * stays readable. The shared request on top is every step's request until that step's own Edit is
 * used. One card for every step, with the single sheet's Preview / Edit / Remove; Preview asks
 * which step, because the requests may differ. The files are the single sheet's checkboxes, and
 * every step gets the ticked ones. One primary button starts the kept steps. Esc cancels.
 */
@Component({
  selector: 'app-workflow-run-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon, SpinnerComponent, AgentIconComponent, WorkflowGraphComponent],
  providers: [provideIcons({ heroArrowRight, heroChevronRight, heroMinusCircle, heroPaperClip, heroXMark })],
  host: { '(keydown.escape)': 'cancel()' },
  template: `
    @if (review(); as r) {
      <section
        #sheet
        role="region"
        [attr.aria-label]="title()"
        tabindex="-1"
        class="mb-2 rounded-2xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
      >
        <div class="flex items-start justify-between gap-3">
          <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">{{ title() }}</h2>
          <button
            type="button"
            (click)="cancel()"
            class="rounded-sm p-0.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200"
            aria-label="Cancel handoff"
          >
            <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
          </button>
        </div>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          Each Agent starts a new conversation in the background. You stay here.
        </p>

        <label [attr.for]="requestId" class="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300">
          Request
        </label>
        <textarea
          [id]="requestId"
          rows="2"
          class="mt-1 block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          [ngModel]="r.request"
          (ngModelChange)="runs.setRequest(sessionId(), $event)"
          [disabled]="r.phase === 'starting'"
        ></textarea>

        @switch (r.phase) {
          @case ('loading') {
            <p class="mt-3 flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
              <app-spinner size="sm" />
              Preparing context from this conversation…
            </p>
          }
          @case ('error') {
            <p class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ r.error }}</p>
          }
          @default {
            <ul role="list" class="mt-3 divide-y divide-gray-200 border-y border-gray-200 dark:divide-gray-700 dark:border-gray-700">
              @for (row of stepRows(); track row.step.stepId) {
                <li class="py-2">
                  <div class="flex items-center gap-3">
                    <app-agent-icon [agentId]="row.step.agentId" [iconUrl]="row.iconUrl" [emoji]="row.emoji" [size]="28" />
                    <div class="min-w-0 flex-1">
                      <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ row.label }}</p>
                      @if (row.unavailable) {
                        <p class="text-xs/5 text-state-danger-700 dark:text-state-danger-300">
                          You can't use this step's Agent. Remove the step, or ask its owner to share it.
                        </p>
                      } @else {
                        <p class="truncate text-xs/5 text-gray-600 dark:text-gray-400">
                          {{ row.modelName }}@if (row.ownRequest) { · Own request }
                        </p>
                      }
                    </div>
                    @if (!row.unavailable) {
                    <button
                      type="button"
                      class="text-sm/6 font-medium text-primary-accessible underline-offset-2 dark:text-primary-accessible-dark hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50"
                      [attr.aria-expanded]="editingStep() === row.step.stepId"
                      [attr.aria-controls]="stepEditorId(row.step.stepId)"
                      [attr.aria-label]="'Edit the request for ' + row.step.agentName"
                      [disabled]="r.phase === 'starting'"
                      (click)="toggleStepEditor(row.step.stepId)"
                    >
                      {{ editingStep() === row.step.stepId ? 'Done' : 'Edit' }}
                    </button>
                    }
                    @if (stepRows().length > 1) {
                      <button
                        type="button"
                        [id]="removeId(row.step.stepId)"
                        class="rounded-sm p-0.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200"
                        [attr.aria-label]="removeLabel(row)"
                        [disabled]="r.phase === 'starting'"
                        (click)="removeStep(row.step.stepId)"
                      >
                        <ng-icon name="heroMinusCircle" class="size-5" aria-hidden="true" />
                      </button>
                    }
                  </div>
                  @if (row.changedLine; as line) {
                    <div class="mt-1 pl-10 text-xs/5 text-gray-600 dark:text-gray-400">
                      <p>{{ line }}</p>
                      @if (isOwner()) {
                        <button
                          type="button"
                          class="mt-0.5 font-medium text-primary-accessible underline-offset-2 dark:text-primary-accessible-dark hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50"
                          [disabled]="!!r.reviewingStep"
                          [attr.aria-label]="'Mark ' + row.label + ' as reviewed'"
                          (click)="markReviewed(row)"
                        >
                          {{ r.reviewingStep === row.stepName ? 'Marking as reviewed…' : 'Mark as reviewed' }}
                        </button>
                      }
                    </div>
                  }
                  @if (editingStep() === row.step.stepId) {
                    <div [id]="stepEditorId(row.step.stepId)" class="mt-2 pl-8">
                      <label [attr.for]="stepEditorId(row.step.stepId) + '-text'" class="block text-xs font-medium text-gray-700 dark:text-gray-300">
                        Request for {{ row.step.agentName }}
                      </label>
                      <textarea
                        [id]="stepEditorId(row.step.stepId) + '-text'"
                        rows="2"
                        class="mt-1 block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                        [ngModel]="row.step.ownRequest ?? r.request"
                        (ngModelChange)="runs.setStepRequest(sessionId(), row.step.stepId, $event)"
                      ></textarea>
                      @if (row.ownRequest) {
                        <button
                          type="button"
                          class="mt-1 text-sm/6 font-medium text-primary-accessible underline-offset-2 dark:text-primary-accessible-dark hover:underline"
                          (click)="runs.setStepRequest(sessionId(), row.step.stepId, null)"
                        >
                          Use the shared request
                        </button>
                      }
                    </div>
                  }
                </li>
              }
            </ul>
            <p class="text-sm/6 text-gray-700 empty:hidden dark:text-gray-300" [class.mt-2]="!!removalNote()" role="status">{{ removalNote() }}</p>

            @if (r.workflow) {
              <button
                type="button"
                class="mt-2 flex items-center gap-1.5 text-sm/6 font-medium text-primary-accessible underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:focus-visible:outline-primary-accessible-dark"
                [attr.aria-expanded]="graphOpen()"
                [attr.aria-controls]="graphOpen() ? graphId : null"
                (click)="graphOpen.set(!graphOpen())"
              >
                <ng-icon name="heroChevronRight" class="size-4 shrink-0 transition-transform motion-reduce:transition-none" [class.rotate-90]="graphOpen()" aria-hidden="true" />
                {{ graphOpen() ? 'Hide the graph' : 'Show as a graph' }}
              </button>
              @if (graphOpen() && graph(); as g) {
                <app-workflow-graph
                  [id]="graphId"
                  class="mt-2 overflow-hidden rounded-xl border border-gray-200 md:h-64 dark:border-gray-700"
                  [stages]="g.stages"
                  [edges]="g.edges"
                  [start]="g.start"
                  [result]="g.result"
                  [stepCount]="g.stepCount"
                  [maxSteps]="maxSteps"
                  label="Graph of the steps to start"
                  mode="run"
                  [status]="g.status"
                  [finished]="g.finished"
                />
              }
            }

            @if (r.phase === 'blocked') {
              <p class="mt-3 text-sm/6 text-gray-700 dark:text-gray-300" role="status">
                Remove the steps you can't use to continue.
              </p>
            } @else {
            <div class="mt-3 rounded-lg bg-gray-50 px-3 py-2 dark:bg-white/5">
              @if (r.brief?.status === 'writing') {
                @if (!r.brief.text) {
                  <p class="flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
                    <app-spinner size="sm" />
                    Writing one brief for all {{ keptCount() }}…
                  </p>
                } @else {
                  <p class="text-sm/6 text-gray-700 dark:text-gray-300" role="status">Writing one brief for all {{ keptCount() }}…</p>
                  <pre tabindex="0" role="region" aria-label="Brief so far" aria-busy="true" class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-2 font-mono text-xs text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-gray-800 dark:text-gray-200">{{ r.brief.text }}</pre>
                }
                <button type="button" class="mt-2 text-sm/6 font-medium text-primary-accessible underline-offset-2 dark:text-primary-50 hover:underline" (click)="runs.useExcerpt(sessionId())">
                  Use the conversation excerpt instead
                </button>
              } @else if (r.cardRemoved) {
                <p class="flex flex-wrap items-center gap-x-2 text-sm/6 text-gray-600 dark:text-gray-400">
                  No context from this conversation will be sent.
                  <button type="button" class="font-medium text-primary-accessible underline-offset-2 dark:text-primary-50 hover:underline" (click)="runs.setCardRemoved(sessionId(), false)">
                    Add it back
                  </button>
                </p>
              } @else if (!r.card) {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">This conversation has no context to send yet.</p>
              } @else {
                <button
                  type="button"
                  class="flex w-full items-center gap-1.5 text-left text-sm/6 text-gray-700 dark:text-gray-300"
                  [attr.aria-expanded]="cardOpen()"
                  [attr.aria-controls]="cardId"
                  (click)="cardOpen.set(!cardOpen())"
                >
                  <ng-icon name="heroChevronRight" class="size-4 shrink-0 transition-transform" [class.rotate-90]="cardOpen()" aria-hidden="true" />
                  <span>
                    @if (r.brief?.status === 'written') {
                      One brief for all {{ keptCount() }}
                    } @else {
                      Context from this conversation
                      @if (coverage(); as c) { · {{ c }} }
                    }
                    · ~{{ tokensLabel() }} tokens
                  </span>
                </button>
                @if (cardOpen()) {
                  <div [id]="cardId" class="mt-2">
                    @if (editingCard()) {
                      <label [attr.for]="cardEditorId" class="sr-only">Context card</label>
                      <textarea
                        [id]="cardEditorId"
                        rows="10"
                        class="block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                        [ngModel]="r.card"
                        (ngModelChange)="runs.setCard(sessionId(), $event)"
                      ></textarea>
                    } @else {
                      <!-- Scrollable, so focusable: keyboard users scroll it with the arrow keys (axe scrollable-region-focusable). -->
                      <pre tabindex="0" role="region" aria-label="Context card" class="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-2 font-mono text-xs text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-gray-800 dark:text-gray-200">{{ r.card }}</pre>
                    }
                    <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm/6">
                      <button type="button" class="font-medium text-primary-accessible underline-offset-2 dark:text-primary-50 hover:underline" (click)="previewOpen.set(!previewOpen())" [attr.aria-expanded]="previewOpen()" [attr.aria-controls]="previewId">
                        {{ previewOpen() ? 'Hide full prompt' : 'Preview full prompt' }}
                      </button>
                      <button type="button" class="font-medium text-primary-accessible underline-offset-2 dark:text-primary-50 hover:underline" (click)="editingCard.set(!editingCard())">
                        {{ editingCard() ? 'Done editing' : 'Edit' }}
                      </button>
                      <button type="button" class="font-medium text-primary-accessible underline-offset-2 dark:text-primary-50 hover:underline" (click)="runs.setCardRemoved(sessionId(), true)">
                        Remove
                      </button>
                    </div>
                  </div>
                }
              }
              @if (r.brief?.status === 'fallback' && !r.cardRemoved && r.card) {
                <p class="mt-1 text-xs text-gray-600 dark:text-gray-400">{{ fallbackNote() }}</p>
              }
              @if (previewOpen() && ((!r.cardRemoved && r.card) || r.fileIds.length) && r.brief?.status !== 'writing') {
                <div [id]="previewId" class="mt-2">
                  <label [attr.for]="previewPickerId" class="block text-xs font-medium text-gray-700 dark:text-gray-300">Show the prompt for</label>
                  <select
                    [id]="previewPickerId"
                    class="mt-1 block rounded-lg border border-gray-300 bg-white py-1 pr-8 pl-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    [ngModel]="previewStepId()"
                    (ngModelChange)="previewStep.set($event)"
                  >
                    @for (row of stepRows(); track row.step.stepId) {
                      <option [value]="row.step.stepId">{{ row.step.agentName }}</option>
                    }
                  </select>
                  <pre tabindex="0" role="region" [attr.aria-label]="'Full prompt ' + previewAgentName() + ' will receive'" class="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-2 font-mono text-xs text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-gray-800 dark:text-gray-200">{{ prompt() }}</pre>
                </div>
              }
            </div>
            }
          }
        }

        @if (fileRule() === 'one' && !r.files?.length) {
          <p class="mt-3 text-xs/5 text-gray-600 dark:text-gray-400">
            This workflow takes one file from this conversation. Attach it with the + button, then
            <button
              type="button"
              class="font-medium text-primary-accessible underline-offset-2 dark:text-primary-accessible-dark hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
              (click)="runs.refreshFiles(sessionId())"
            >
              look again
            </button>.
          </p>
        }
        @if (r.files?.length && fileRule() !== 'none') {
          <fieldset class="mt-3">
            <legend class="text-xs font-medium text-gray-700 dark:text-gray-300">Files from this conversation</legend>
            <p [id]="filesHintId" class="text-xs text-gray-600 dark:text-gray-400">
              @if (fileRule() === 'one') {
                This workflow takes exactly one file. Tick the one every Agent should open.
              } @else {
                Every Agent can open the ones you tick. Up to {{ maxFiles }}.
              }
            </p>
            <ul class="mt-1 space-y-1" [attr.aria-describedby]="filesHintId">
              @for (file of r.files; track file.uploadId) {
                <li>
                  <label class="flex items-center gap-2 text-sm/6 text-gray-800 dark:text-gray-200">
                    <input
                      type="checkbox"
                      class="size-4 rounded-sm border-gray-300 text-primary-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600"
                      [checked]="r.fileIds.includes(file.uploadId)"
                      [disabled]="r.phase === 'starting' || (fileRule() !== 'one' && !r.fileIds.includes(file.uploadId) && r.fileIds.length >= maxFiles)"
                      (change)="toggleFile(file.uploadId, $event)"
                    />
                    <span class="min-w-0 truncate">{{ file.filename }}</span>
                    <span class="shrink-0 text-xs text-gray-600 dark:text-gray-400">{{ fileMeta(file) }}</span>
                  </label>
                </li>
              }
            </ul>
          </fieldset>
        }

        @if (attachmentCount() > 0) {
          <p class="mt-2 flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
            <ng-icon name="heroPaperClip" class="size-4" aria-hidden="true" />
            {{ attachmentCount() }} {{ attachmentCount() === 1 ? 'attachment goes' : 'attachments go' }} with every request
          </p>
        }

        @if (r.phase === 'ready' || r.phase === 'starting') {
          <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400">{{ estimateLine() }}</p>
        }
        @if (mergeInstruction(); as instruction) {
          <div class="mt-3 rounded-lg bg-gray-50 px-3 py-2 dark:bg-white/5" data-testid="run-merge-notice">
            <p class="text-sm/6 text-gray-800 dark:text-gray-200">
              <span class="font-medium">Then this conversation merges the results:</span> {{ instruction }}
            </p>
            <p class="mt-0.5 text-xs/5 text-gray-600 dark:text-gray-400">
              The merge starts by itself when every Agent finishes, if this conversation is open and
              nothing is typed in its composer. Otherwise the results wait on your composer.
            </p>
          </div>
        }
        @if (r.phase === 'ready' && r.error) {
          <p class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ r.error }}</p>
        }

        <div class="mt-4 flex justify-end gap-2">
          <button
            type="button"
            (click)="cancel()"
            class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-white/10"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="start()"
            [disabled]="!startable()"
            class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Start {{ agents() }}
            <ng-icon name="heroArrowRight" class="size-4" aria-hidden="true" />
          </button>
        </div>
      </section>
    }
  `,
})
export class WorkflowRunSheetComponent {
  protected readonly runs = inject(WorkflowRunService);
  private readonly state = inject(WorkflowRunStateService);
  private readonly models = inject(ModelService);
  private readonly injector = inject(Injector);

  /** The source conversation: the one being viewed. */
  readonly sessionId = input.required<string>();

  private static nextId = 0;
  private readonly uid = WorkflowRunSheetComponent.nextId++;
  protected readonly requestId = `run-request-${this.uid}`;
  protected readonly cardId = `run-card-${this.uid}`;
  protected readonly cardEditorId = `run-card-editor-${this.uid}`;
  protected readonly previewId = `run-preview-${this.uid}`;
  protected readonly previewPickerId = `run-preview-picker-${this.uid}`;
  protected readonly filesHintId = `run-files-hint-${this.uid}`;
  protected readonly maxFiles = MAX_SHARED_FILES;
  protected readonly maxSteps = MAX_WORKFLOW_STEPS;
  protected readonly graphId = `run-sheet-graph-${this.uid}`;
  /** The small graph (PR-D.5b), behind a disclosure so the sheet stays short. */
  protected readonly graphOpen = signal(false);

  protected readonly cardOpen = signal(false);
  protected readonly editingCard = signal(false);
  protected readonly previewOpen = signal(false);
  protected readonly editingStep = signal<string | null>(null);
  /** What the last Remove took with it, or why it took nothing (PR-D.5a). */
  protected readonly removalNote = signal('');
  protected readonly previewStep = signal<string | null>(null);

  private readonly sheet = viewChild<ElementRef<HTMLElement>>('sheet');

  protected readonly review = computed(() => this.state.reviewFor(this.sessionId()));

  protected readonly keptCount = computed(() => {
    const r = this.review();
    // While the run loads, the sheet already names what the composer mentioned.
    if (!r) return 0;
    if (r.phase === 'loading' || r.phase === 'error') return r.draft.agents.length - (r.removedSteps?.length ?? 0);
    return keptSteps(r).length;
  });

  protected readonly agents = computed(() => agentsLabel(this.keptCount()));
  /** A saved workflow's sheet is titled with its name (§8.3); a run of mentions says who it goes to. */
  protected readonly title = computed(() => this.review()?.workflow?.document.workflow.title || `Hand off to ${this.agents()}`);

  /** The saved workflow's `files` rule, or undefined for a run of mentions. */
  protected readonly fileRule = computed(() => this.review()?.workflow?.document.workflow.files);

  /** A merging Result's instruction, as the server's sheet words it (PR-D.5b). */
  protected readonly mergeInstruction = computed(() => this.review()?.workflow?.document.sheet.mergeInstruction?.trim() || null);

  /** The steps it would start, on the designer's graph (PR-D.5b). */
  protected readonly graph = computed(() => {
    const r = this.review();
    const rows = this.stepRows();
    const names = new Map(rows.map((row) => [row.step.stepId, row.modelName]));
    return r ? reviewGraph(r, (stepId) => names.get(stepId) ?? 'Your selected model') : null;
  });

  /** Mark as reviewed is the workflow owner's: it rewrites the file (§10). */
  protected readonly isOwner = computed(() => !!this.review()?.workflow?.document.workflow.isOwned);

  protected readonly stepRows = computed<StepRow[]>(() => {
    const r = this.review();
    if (!r) return [];
    const catalog = this.models.availableModels();
    const sendModel = this.models.selectedModel();
    return keptSteps(r).map((step) => {
      const agent = r.draft.agents.find((candidate) => candidate.agentId === step.agentId);
      const model = step.modelId ? catalog.find((m) => m.modelId === step.modelId) : sendModel;
      const saved = step.workflowStep;
      const unavailable = !!saved?.unavailable;
      return {
        step,
        label: unavailable ? `Unavailable Agent (${saved?.name})` : step.agentName,
        modelName: model?.modelName || step.modelId || 'Your selected model',
        emoji: agent?.emoji,
        iconUrl: agent?.iconUrl,
        ownRequest: step.ownRequest !== null,
        unavailable,
        stepName: saved?.name,
        changedLine: saved && !unavailable && saved.changed ? changedLine(saved.publishedVersion, saved.runsDraft) : undefined,
        readers: readersOf(r, step.stepId).map((reader) => readerLabel(reader)),
      };
    });
  });

  /** The step Preview shows: the one picked, else the first kept. */
  protected readonly previewStepId = computed(() => {
    const rows = this.stepRows();
    const picked = this.previewStep();
    return rows.some((row) => row.step.stepId === picked) ? picked : (rows[0]?.step.stepId ?? null);
  });

  protected readonly previewAgentName = computed(
    () => this.stepRows().find((row) => row.step.stepId === this.previewStepId())?.step.agentName ?? '',
  );

  protected readonly prompt = computed(() => {
    const r = this.review();
    const stepId = this.previewStepId();
    return r && stepId ? this.runs.previewPrompt(r, stepId) : '';
  });

  protected readonly coverage = computed(() => {
    const turns = this.review()?.created?.turnsCovered;
    if (!turns) return '';
    return turns.first === turns.last ? `turn ${turns.first}` : `turns ${turns.first}–${turns.last}`;
  });

  protected readonly tokensLabel = computed(() => {
    const r = this.review();
    if (!r) return '0';
    // Same estimate as the server: four characters a token.
    const tokens = Math.ceil(r.card.length / 4);
    return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
  });

  protected readonly attachmentCount = computed(() => {
    const draft = this.review()?.draft;
    return (draft?.fileUploadIds?.length ?? 0) + (draft?.folders?.length ?? 0);
  });

  /**
   * "About $0.90 to start, before any follow-up (estimate)": every kept step's first message, at
   * its model's input rate and tokenizer. A step whose Agent names no model is priced on the
   * send's model. Anything unpriced says so rather than show a wrong number.
   */
  protected readonly estimateLine = computed(() => {
    const r = this.review();
    if (!r) return '';
    const sendModel = this.models.selectedModel();
    const byId = new Map((r.files ?? []).map((file) => [file.uploadId, file]));
    const usd = estimateRunInputUsd({
      steps: keptSteps(r).map((step) => ({
        modelId: step.modelId ?? sendModel?.modelId ?? null,
        inputPricePerMillionTokens: step.modelId
          ? (step.inputPricePerMillionTokens ?? null)
          : (sendModel?.inputPricePerMillionTokens ?? null),
        request: stepRequest(r, step),
      })),
      card: r.cardRemoved ? '' : r.card,
      fileTokens: r.fileIds.map((id) => byId.get(id)?.estimatedTokens ?? null),
    });
    return usd === null
      ? 'Cost estimate unavailable: a model’s price or a file’s size isn’t known yet'
      : `${formatEstimateUsd(usd)} to start, before any follow-up (estimate)`;
  });

  protected readonly startable = computed(() => {
    const r = this.review();
    return !!r && canStart(r);
  });

  protected readonly fallbackNote = computed(() =>
    this.review()?.brief?.fallbackReason === 'preempted'
      ? 'The brief was stopped, so the conversation excerpt is shown instead.'
      : 'A brief couldn’t be written, so the conversation excerpt is shown instead.',
  );

  constructor() {
    // Move focus into the sheet when it opens, so Esc and the keyboard land here and a screen
    // reader hears the region's name.
    let focusedFor: string | undefined;
    let lastBriefStatus: string | undefined;
    effect(() => {
      const r = this.review();
      const el = this.sheet()?.nativeElement;
      const key = r ? `${this.sessionId()}:${r.draft.agents.map((a) => a.agentId).join(',')}` : undefined;
      if (el && key && key !== focusedFor) {
        focusedFor = key;
        el.focus();
      }
      // A brief the user watched stream in stays open once written.
      const briefStatus = r?.brief?.status;
      if (briefStatus === 'written' && lastBriefStatus === 'writing') this.cardOpen.set(true);
      lastBriefStatus = briefStatus;
      if (!r) {
        focusedFor = undefined;
        this.cardOpen.set(false);
        this.editingCard.set(false);
        this.previewOpen.set(false);
        this.editingStep.set(null);
        this.previewStep.set(null);
        this.removalNote.set('');
        this.graphOpen.set(false);
      }
    });
  }

  protected stepEditorId(stepId: string): string {
    return `run-step-editor-${this.uid}-${stepId}`;
  }

  protected removeId(stepId: string): string {
    return `run-step-remove-${this.uid}-${stepId}`;
  }

  protected toggleStepEditor(stepId: string): void {
    this.editingStep.set(this.editingStep() === stepId ? null : stepId);
  }

  /** Remove's name, which says when the steps reading this one go with it (PR-D.5a). */
  protected removeLabel(row: StepRow): string {
    const n = row.readers.length;
    if (n === 0) return `Remove ${row.label}`;
    return `Remove ${row.label} and the ${n === 1 ? 'step' : `${n} steps`} that ${n === 1 ? 'reads' : 'read'} it`;
  }

  /**
   * Remove a step, and every step that reads it (PR-D.5a), saying which went. Then move focus to
   * the Remove of the step now in its place (or the last one's), so the keyboard user stays in the
   * list rather than falling back to the page. With one step left there is no Remove, so focus
   * goes to the sheet. A step every other step reads can't be removed, and the sheet says so.
   */
  protected removeStep(stepId: string): void {
    const rows = this.stepRows();
    const index = rows.findIndex((row) => row.step.stepId === stepId);
    const row = rows[index];
    const r = this.review();
    if (!row || !r) return;
    const removed = removalOf(r, stepId);
    if (removed.length === 0) {
      this.removalNote.set(
        row.readers.length > 0 ? `${row.label} can’t be removed: every other step reads it.` : '',
      );
      return;
    }
    if (removed.includes(this.editingStep() ?? '')) this.editingStep.set(null);
    this.runs.removeStep(this.sessionId(), stepId);
    this.removalNote.set(row.readers.length > 0 ? `Removed ${row.label}, and ${joinNames(row.readers)}, which read it.` : '');
    afterNextRender(
      () => {
        const left = this.stepRows();
        const neighbour = left[Math.min(index, left.length - 1)];
        const next = left.length > 1 && neighbour ? document.getElementById(this.removeId(neighbour.step.stepId)) : null;
        (next ?? this.sheet()?.nativeElement)?.focus();
      },
      { injector: this.injector },
    );
  }

  protected cancel(): void {
    this.runs.cancel(this.sessionId());
  }

  protected toggleFile(uploadId: string, event: Event): void {
    this.runs.setFileShared(this.sessionId(), uploadId, (event.target as HTMLInputElement).checked);
  }

  protected fileMeta(file: RunShareableFile): string {
    if (file.pages) return `${file.pages} ${file.pages === 1 ? 'page' : 'pages'}`;
    return formatFileSize(file.sizeBytes);
  }

  protected start(): void {
    void this.runs.start(this.sessionId());
  }

  protected markReviewed(row: StepRow): void {
    if (row.stepName) void this.runs.markReviewed(this.sessionId(), row.stepName);
  }
}

/**
 * §10's line for a saved step whose Agent published a newer version since it was added. There is
 * no "See what changed": the version diff routes are admin-only (§20.2).
 */
export function changedLine(publishedVersion: number | undefined, runsDraft: boolean): string {
  const now = publishedVersion !== undefined ? `, now version ${publishedVersion}` : '';
  const line = `This Agent has changed since it was added. A run uses its published version${now}.`;
  return runsDraft ? `${line} You own it, so your run uses its draft.` : line;
}

/** A step as the sheet names it when another's Remove takes it too. */
function readerLabel(step: RunReviewStep): string {
  return step.agentName || step.workflowStep?.name || 'a step';
}

/** "A", "A and B", "A, B and C". */
function joinNames(names: readonly string[]): string {
  if (names.length <= 1) return names[0] ?? '';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}
