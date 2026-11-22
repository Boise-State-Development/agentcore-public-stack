import { ChangeDetectionStrategy, Component, Injector, afterNextRender, computed, inject, output, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { RouterLink } from '@angular/router';
import { heroArrowDown, heroArrowUp, heroExclamationTriangle, heroMagnifyingGlass, heroPlus, heroTrash } from '@ng-icons/heroicons/outline';

import { AgentIconComponent } from '../../agents/components/agent-icon.component';
import { MentionableAgent } from '../../agents/services/agent-mention.service';
import { DraftStep, MAX_CHECKLIST_FIELD_SETS, isModelDraftStep } from '../workflow-draft';
import { RESULT_NODE, START_NODE, WorkflowDesignerStore } from '../workflow-designer.store';
import { MAX_WORKFLOW_STEPS, WorkflowFiles } from '../workflow.models';

/** Why the server says this user can't run a prompt step's model (`ModelView.reason`). */
export function modelReasonCopy(reason: string | undefined): string {
  switch (reason) {
    case 'no_access':
      return 'You can’t use this model. Choose another.';
    case 'no_catalog_row':
      return 'This model isn’t offered here. Choose another.';
    case 'retired':
      return 'This model has been retired. Choose another.';
    case 'unsupported':
      return 'This model can’t run this step. Choose a text model that reads documents.';
    default:
      return 'This model couldn’t be checked. Try again, or choose another.';
  }
}

/** Why the server says a checklist step's field set can't run (`FieldSetView.reason`). */
export function fieldSetReasonCopy(reason: string | undefined): string {
  switch (reason) {
    case 'missing':
      return 'No longer in this workflow’s skill. Remove it, or add it back to the skill.';
    case 'list_field':
      return 'Has a list field, which a checklist step can’t fill yet. Remove it, or change the field’s type.';
    default:
      return 'Couldn’t be read. Try again, or remove it.';
  }
}

const FILE_CHOICES: { value: WorkflowFiles; label: string; hint: string }[] = [
  { value: 'none', label: 'No files', hint: 'The starter only writes a request.' },
  { value: 'one', label: 'Exactly one file', hint: 'The starter attaches one, such as the proposal.' },
  { value: 'any', label: 'Any files', hint: 'None, one or several.' },
];

/**
 * The Step tab (docs/specs/workflow-designer.md §5): edits the selected card. Agent steps, prompt
 * steps (§21.3, behind `features.promptSteps`), checklist steps (§22.3, behind
 * `features.checklistSteps`) and the two fixed cards.
 *
 * A checklist step picks its model as a prompt step does, and one or more field sets from this
 * workflow's skill, in the order it runs them (Move up / Move down, as the field-set editor moves
 * fields). It always reads the run's files, since its sets are filled in from them. Its text is
 * guidance added to every set's call, never the run's request. The sets are listed by filename
 * from the skill; the server reads and judges them on every preview.
 *
 * A prompt step is one model call with no Agent: it has a name of its own (several can share a
 * model), a model from the ones this user can use that run a prompt step (`promptModelChoices`),
 * and an instruction, which the file keeps as the step's `## Step:` section.
 *
 * Reads decide a step's stage (§4.1): a step can read any other step's result except one that
 * already reads it, directly or on the way, which is shown disabled with why, so the form can't
 * make the cycle the format refuses. The Result sets how it comes back (card, or a merge with its
 * instruction) and which steps' results it includes. The SPA never writes the file: the server's
 * `PUT` does, and refuses what the format refuses.
 *
 * A dense form, so it stays plain: the canvas is the page's one bold surface.
 */
@Component({
  selector: 'app-workflow-step-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentIconComponent, RouterLink],
  providers: [provideIcons({ heroArrowDown, heroArrowUp, heroExclamationTriangle, heroMagnifyingGlass, heroPlus, heroTrash })],
  template: `
    @switch (kind()) {
      @case ('start') {
        <section class="space-y-6" aria-labelledby="wf-step-heading">
          <div>
            <h2 id="wf-step-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Starts with</h2>
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">What a person starting this workflow is asked for.</p>
          </div>

          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Files</legend>
            <div class="mt-2 space-y-2">
              @for (choice of fileChoices; track choice.value) {
                <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input
                    type="radio"
                    name="wf-files"
                    [value]="choice.value"
                    [checked]="draft().files === choice.value"
                    [disabled]="!editable()"
                    (change)="store.setFiles(choice.value)"
                    class="mt-1 size-4 border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span>
                    {{ choice.label }}
                    <span class="block text-xs/5 text-gray-600 dark:text-gray-400">{{ choice.hint }}</span>
                  </span>
                </label>
              }
            </div>
          </fieldset>

          <div>
            <label for="wf-request" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Default request</label>
            <textarea
              id="wf-request"
              rows="4"
              [value]="draft().request"
              [readOnly]="!editable()"
              (input)="store.setRequest(textOf($event))"
              placeholder="Review this proposal against the solicitation."
              aria-describedby="wf-request-help"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:focus:ring-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            ></textarea>
            <p id="wf-request-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
              The sheet opens with this filled in, and the starter can change it. Every step gets it unless it has its own.
            </p>
          </div>
        </section>
      }

      @case ('result') {
        <section class="space-y-6" aria-labelledby="wf-step-heading">
          <div>
            <h2 id="wf-step-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Result</h2>
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">What comes back to the conversation that started the run.</p>
          </div>
          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">How it comes back</legend>
            <div class="mt-2 space-y-2">
              <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                <input
                  type="radio"
                  name="wf-result"
                  value="card"
                  [checked]="draft().result.mode === 'card'"
                  [disabled]="!editable()"
                  (change)="store.setResultMode('card')"
                  [class]="radioClass"
                />
                <span>
                  One card on the composer
                  <span class="block text-xs/5 text-gray-600 dark:text-gray-400">The results, sent with your next message.</span>
                </span>
              </label>
              <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                <input
                  type="radio"
                  name="wf-result"
                  value="merge"
                  [checked]="draft().result.mode === 'merge'"
                  [disabled]="!editable()"
                  (change)="useMerge()"
                  [class]="radioClass"
                />
                <span>
                  A merge that starts by itself
                  <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
                    Once every step has finished, the conversation that started the run gets the results and this instruction, and answers.
                  </span>
                </span>
              </label>
            </div>
            @if (draft().result.mode === 'merge') {
              <label for="wf-result-instruction" class="mt-3 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Merge instruction</label>
              <textarea
                id="wf-result-instruction"
                rows="4"
                [value]="draft().result.instruction ?? ''"
                [readOnly]="!editable()"
                (input)="store.setResultInstruction(textOf($event))"
                placeholder="Write one report: the inline notes first, then the audit table."
                aria-describedby="wf-result-instruction-help"
                [class]="textareaClass"
              ></textarea>
              <p id="wf-result-instruction-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                Leave it blank and the conversation is only asked to combine the results.
              </p>
            }
          </fieldset>

          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">It includes</legend>
            @if (draft().steps.length) {
              <div class="mt-2 space-y-1.5">
                @for (step of draft().steps; track step.key) {
                  @let included = includes(step.name);
                  @let last = included && store.resultSteps().length === 1;
                  <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                    <input
                      type="checkbox"
                      [checked]="included"
                      [disabled]="!editable() || last"
                      (change)="store.toggleResultRead(step.name)"
                      [attr.aria-describedby]="last ? 'wf-result-last' : null"
                      [class]="checkboxClass + ' mt-1'"
                    />
                    <span>
                      {{ store.stepLabel(step) }}’s result
                      <span class="block text-xs/5 text-gray-600 dark:text-gray-400">Stage {{ store.stageOf(step.key) }}</span>
                    </span>
                  </label>
                }
              </div>
              <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400" [attr.id]="store.resultSteps().length === 1 ? 'wf-result-last' : null">
                @if (draft().result.reads === null) {
                  Every step’s result, in the order the steps are listed, including steps you add later.
                } @else if (store.resultSteps().length === 1) {
                  One step’s result. The result always includes at least one.
                } @else {
                  {{ store.resultSteps().length }} of {{ draft().steps.length }} steps’ results, in the order the steps are listed.
                }
              </p>
            } @else {
              <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">Each step’s result, once you add steps.</p>
            }
          </fieldset>
        </section>
      }

      @case ('step') {
        @if (step(); as step) {
          <section class="space-y-6" aria-labelledby="wf-step-heading">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <h2 id="wf-step-heading" class="truncate text-base/7 font-semibold text-gray-900 dark:text-white">{{ store.stepLabel(step) }}</h2>
                <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                  {{ kindLabel(step) }} · stage {{ stageNumber() }}@if (model()) { · {{ model() }} }
                </p>
              </div>
            </div>

            @if (store.promptStepsOn || isModelStep(step)) {
              <fieldset>
                <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Kind</legend>
                <div class="mt-2 space-y-2">
                  <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                    <input
                      id="wf-kind-agent"
                      type="radio"
                      name="wf-step-kind"
                      [checked]="step.kind === 'agent'"
                      [disabled]="!editable()"
                      (change)="store.setStepKind(step.key, 'agent')"
                      [class]="radioClass"
                    />
                    <span>
                      Agent step
                      <span class="block text-xs/5 text-gray-600 dark:text-gray-400">An Agent answers in a conversation of its own, with its tools.</span>
                    </span>
                  </label>
                  <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                    <input
                      id="wf-kind-prompt"
                      type="radio"
                      name="wf-step-kind"
                      [checked]="step.kind === 'prompt'"
                      [disabled]="!editable() || !store.promptStepsOn"
                      (change)="store.setStepKind(step.key, 'prompt')"
                      [class]="radioClass"
                    />
                    <span>
                      Prompt step
                      <span class="block text-xs/5 text-gray-600 dark:text-gray-400">One model call over what it reads. No Agent, no tools, no conversation.</span>
                    </span>
                  </label>
                  @if (store.checklistStepsOn || step.kind === 'checklist') {
                    <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                      <input
                        id="wf-kind-checklist"
                        type="radio"
                        name="wf-step-kind"
                        [checked]="step.kind === 'checklist'"
                        [disabled]="!editable() || !store.checklistStepsOn"
                        (change)="store.setStepKind(step.key, 'checklist')"
                        [class]="radioClass"
                      />
                      <span>
                        Checklist step
                        <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
                          Fills in field sets from the run’s files, one model call each, with a quote for every value.
                        </span>
                      </span>
                    </label>
                  }
                </div>
              </fieldset>
            }

            @if (isModelStep(step)) {
              <div>
                <label for="wf-step-name" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Name</label>
                <input
                  id="wf-step-name"
                  type="text"
                  maxlength="40"
                  [value]="step.autoName ? '' : nameOf(step)"
                  [readOnly]="!editable()"
                  (change)="rename(step, $event)"
                  placeholder="Budget check"
                  aria-describedby="wf-step-name-help"
                  [class]="inputClass"
                />
                <p id="wf-step-name-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                  What the run and the Result call this step. Other steps read it by this name.
                </p>
              </div>

              <fieldset>
                <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Model</legend>
                @if (modelProblem(); as problem) {
                  <p class="mt-1 flex items-center gap-1.5 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                    <ng-icon name="heroExclamationTriangle" class="size-4 shrink-0" aria-hidden="true" />
                    {{ problem }}
                  </p>
                }
                <div class="mt-2 max-h-64 overflow-y-auto rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                  @for (choice of store.promptModels(); track choice.modelId) {
                    @let textOnly = !choice.readsDocuments && step.reads.includes('files');
                    <label
                      class="flex cursor-pointer items-center gap-3 border-b border-gray-100 px-3 py-2 last:border-0 has-checked:bg-gray-100 has-disabled:cursor-not-allowed dark:border-white/5 dark:has-checked:bg-gray-700"
                    >
                      <input
                        type="radio"
                        name="wf-model"
                        [value]="choice.modelId"
                        [checked]="choice.modelId === step.modelId"
                        [disabled]="!editable() || textOnly"
                        (change)="store.choosePromptModel(step.key, choice.modelId)"
                        class="size-4 shrink-0 border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800"
                      />
                      <span class="min-w-0 flex-1">
                        <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ choice.name }}</span>
                        <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">
                          @if (textOnly) {
                            Text only: it can’t be sent the run’s files
                          } @else if (choice.readsDocuments) {
                            Reads the run’s files and earlier results
                          } @else {
                            Text only: reads earlier results, not files
                          }
                        </span>
                      </span>
                    </label>
                  } @empty {
                    <p class="px-3 py-3 text-sm/6 text-gray-600 dark:text-gray-400">No model you can use runs a {{ step.kind === 'checklist' ? 'checklist' : 'prompt' }} step.</p>
                  }
                </div>
              </fieldset>

              @if (step.kind === 'checklist') {
                <fieldset aria-describedby="wf-sets-help">
                  <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Field sets</legend>
                  <p id="wf-sets-help" class="text-xs/5 text-gray-600 dark:text-gray-400">
                    From this workflow’s skill. Each set is one model call, in this order. Up to {{ maxSets }}.
                  </p>
                  @if (!step.fieldSets.length) {
                    <p class="mt-1 flex items-center gap-1.5 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                      <ng-icon name="heroExclamationTriangle" class="size-4 shrink-0" aria-hidden="true" />
                      Choose at least one field set.
                    </p>
                  } @else {
                    <ol role="list" class="mt-2 divide-y divide-gray-100 rounded-2xl border border-gray-200 bg-white dark:divide-white/5 dark:border-gray-700 dark:bg-gray-800" data-testid="wf-chosen-sets">
                      @for (name of step.fieldSets; track name; let i = $index, first = $first, last = $last) {
                        @let judged = store.fieldSetView(name);
                        <li class="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2">
                          <span class="min-w-0 flex-1">
                            <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                              <span class="sr-only">{{ i + 1 }}. </span>{{ store.fieldSetTitle(name) }}
                            </span>
                            @if (judged && !judged.canRun) {
                              <span class="flex items-start gap-1 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                                <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                                {{ setReason(judged.reason) }}
                              </span>
                            } @else {
                              <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">
                                {{ name }}@if (judged?.fieldCount; as count) { · {{ count }} {{ count === 1 ? 'field' : 'fields' }} }
                              </span>
                            }
                          </span>
                          <span class="flex shrink-0 items-center gap-1">
                            @if (store.fieldSetAuthoringOn && editable() && skillId()) {
                              <!-- A new tab, so the draft stays as it is; Refresh the list reads the skill again. -->
                              <a
                                [routerLink]="['/customize/skills', skillId(), 'field-sets', name]"
                                target="_blank"
                                rel="noopener"
                                [attr.aria-label]="'Edit the field set ' + store.fieldSetTitle(name) + ' (opens in a new tab)'"
                                class="rounded-md px-1.5 text-xs/5 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:focus-visible:outline-primary-accessible-dark"
                              >
                                Edit
                              </a>
                            }
                            @if (editable()) {
                              <button
                                type="button"
                                [id]="setControlId('up', name)"
                                (click)="moveSet(step, name, -1)"
                                [disabled]="first"
                                [attr.aria-label]="'Move ' + store.fieldSetTitle(name) + ' up'"
                                [class]="iconButtonClass"
                              >
                                <ng-icon name="heroArrowUp" class="size-4" aria-hidden="true" />
                              </button>
                              <button
                                type="button"
                                [id]="setControlId('down', name)"
                                (click)="moveSet(step, name, 1)"
                                [disabled]="last"
                                [attr.aria-label]="'Move ' + store.fieldSetTitle(name) + ' down'"
                                [class]="iconButtonClass"
                              >
                                <ng-icon name="heroArrowDown" class="size-4" aria-hidden="true" />
                              </button>
                              <button
                                type="button"
                                [id]="setControlId('remove', name)"
                                (click)="removeSet(step, name)"
                                [attr.aria-label]="'Remove ' + store.fieldSetTitle(name) + ' from this step'"
                                [class]="iconButtonClass + ' text-state-danger-700 dark:text-state-danger-400'"
                              >
                                <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                              </button>
                            }
                          </span>
                        </li>
                      }
                    </ol>
                  }

                  @if (editable()) {
                    <div class="mt-3" data-testid="wf-skill-sets">
                      @if (!skillId()) {
                        <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                          A checklist step’s field sets live in this workflow’s skill, which isn’t made until the workflow is.
                          <button type="button" (click)="createCarrier()" [disabled]="store.saving()" [class]="linkButtonClass + ' disabled:opacity-60'">
                            {{ store.saving() ? 'Creating…' : 'Create the workflow now' }}
                          </button>
                        </p>
                      } @else {
                        @switch (skillSets().state) {
                          @case ('loading') {
                            <p class="text-xs/5 text-gray-600 dark:text-gray-400" role="status">Reading this workflow’s field sets…</p>
                          }
                          @case ('error') {
                            <p class="text-xs/5 text-state-danger-700 dark:text-state-danger-300" role="alert">
                              This workflow’s field sets couldn’t be read.
                              <button type="button" (click)="store.loadSkillFieldSets()" [class]="linkButtonClass">Try again</button>
                            </p>
                          }
                          @case ('ready') {
                            @if (addableSets().length) {
                              <p class="text-xs/5 font-medium text-gray-700 dark:text-gray-300">Add from this workflow’s skill</p>
                              <ul role="list" class="mt-1 flex flex-wrap gap-2">
                                @for (name of addableSets(); track name) {
                                  <li>
                                    <button
                                      type="button"
                                      (click)="addSet(step, name)"
                                      [disabled]="step.fieldSets.length >= maxSets"
                                      [attr.aria-label]="'Add the field set ' + store.fieldSetTitle(name)"
                                      class="inline-flex items-center gap-1 rounded-2xl border border-gray-300 bg-white px-2.5 py-1 text-xs/5 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus-visible:outline-primary-accessible-dark"
                                    >
                                      <ng-icon name="heroPlus" class="size-3.5" aria-hidden="true" />
                                      {{ store.fieldSetTitle(name) }}
                                    </button>
                                  </li>
                                }
                              </ul>
                            } @else if (!skillSetNames().length) {
                              <p class="text-xs/5 text-gray-600 dark:text-gray-400">This workflow’s skill has no field sets yet.</p>
                            } @else {
                              <p class="text-xs/5 text-gray-600 dark:text-gray-400">Every field set in this workflow’s skill is on this step.</p>
                            }
                            <p class="mt-2 flex flex-wrap gap-x-3 text-xs/5">
                              @if (store.fieldSetAuthoringOn) {
                                <a
                                  [routerLink]="['/customize/skills', skillId(), 'field-sets', 'new']"
                                  target="_blank"
                                  rel="noopener"
                                  [class]="linkButtonClass"
                                >
                                  New field set<span class="sr-only"> (opens in a new tab)</span>
                                </a>
                              }
                              <button type="button" (click)="store.loadSkillFieldSets()" [class]="linkButtonClass">Refresh the list</button>
                            </p>
                          }
                        }
                      }
                    </div>
                  }
                </fieldset>

                <div>
                  <label for="wf-guidance" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Guidance (optional)</label>
                  <textarea
                    id="wf-guidance"
                    rows="4"
                    [value]="step.ownRequest ?? ''"
                    [readOnly]="!editable()"
                    (input)="setInstruction(step, $event)"
                    placeholder="Answer from the budget justification first. Treat a missing section as a fail."
                    aria-describedby="wf-guidance-help"
                    [class]="textareaClass"
                  ></textarea>
                  <p id="wf-guidance-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                    Added to every set’s call, after its fields. It never takes the run’s request.
                  </p>
                </div>
              } @else {
              <div>
                <label for="wf-instruction" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Instruction</label>
                <textarea
                  id="wf-instruction"
                  rows="5"
                  [value]="step.ownRequest ?? ''"
                  [readOnly]="!editable()"
                  (input)="setInstruction(step, $event)"
                  placeholder="List the three biggest risks in the budget justification, with the page for each."
                  aria-describedby="wf-instruction-help"
                  [class]="textareaClass"
                ></textarea>
                <p id="wf-instruction-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                  What this one call is asked, after the files and results it reads. Left blank, it is asked the run’s request.
                </p>
              </div>
              }
            } @else {

            @if (view()?.runsDraft) {
              <p class="rounded-2xl border border-gray-200 bg-white px-3 py-2 text-xs/5 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
                You own this Agent, so your test runs use its draft. Others run its published version.
              </p>
            }

            <fieldset>
              <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Agent</legend>
              @if (step.agentId && !known()) {
                <p class="mt-1 flex items-center gap-1.5 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                  <ng-icon name="heroExclamationTriangle" class="size-4 shrink-0" aria-hidden="true" />
                  @if (view() && !view()!.canRun) {
                    You can’t use this step’s Agent. Choose another, or remove the step.
                  } @else {
                    {{ store.stepLabel(step) }} isn’t in your Agents or pins.
                  }
                </p>
              }
              <div class="relative mt-2">
                <ng-icon name="heroMagnifyingGlass" class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                <label for="wf-agent-filter" class="sr-only">Find an Agent</label>
                <input
                  id="wf-agent-filter"
                  type="search"
                  [value]="filter()"
                  (input)="filter.set(textOf($event))"
                  placeholder="Find an Agent"
                  [disabled]="!editable()"
                  class="block w-full rounded-2xl border border-gray-300 bg-white py-1.5 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:focus:ring-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400"
                />
              </div>
              <div class="mt-2 max-h-64 overflow-y-auto rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                @for (agent of choices(); track agent.agentId) {
                  <label
                    class="flex cursor-pointer items-center gap-3 border-b border-gray-100 px-3 py-2 last:border-0 has-checked:bg-gray-100 has-disabled:cursor-not-allowed dark:border-white/5 dark:has-checked:bg-gray-700"
                  >
                    <input
                      type="radio"
                      name="wf-agent"
                      [value]="agent.agentId"
                      [checked]="agent.agentId === step.agentId"
                      [disabled]="!editable() || usedElsewhere(agent.agentId)"
                      (change)="choose(agent)"
                      class="size-4 shrink-0 border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800"
                    />
                    <app-agent-icon [agentId]="agent.agentId" [iconUrl]="agent.iconUrl" [emoji]="agent.emoji" [size]="28" />
                    <span class="min-w-0 flex-1">
                      <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ agent.name }}</span>
                      <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">
                        @if (usedElsewhere(agent.agentId)) {
                          Already a step: each Agent can be a step once
                        } @else {
                          {{ agent.group === 'own' ? 'Yours' : 'Pinned' }}@if (agent.subtitle) { · {{ agent.subtitle }} }
                        }
                      </span>
                    </span>
                  </label>
                } @empty {
                  <p class="px-3 py-3 text-sm/6 text-gray-600 dark:text-gray-400">
                    @if (filter()) {
                      No Agent matches “{{ filter() }}”.
                    } @else {
                      No Agents yet. Your own Agents and the ones you pin are listed here.
                    }
                  </p>
                }
              </div>
            </fieldset>

            <fieldset>
              <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Request</legend>
              <div class="mt-2 space-y-2">
                <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input
                    type="radio"
                    name="wf-step-request"
                    [checked]="step.ownRequest === null"
                    [disabled]="!editable()"
                    (change)="store.setOwnRequest(step.key, null)"
                    class="mt-1 size-4 border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span>The run’s request</span>
                </label>
                <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input
                    type="radio"
                    name="wf-step-request"
                    [checked]="step.ownRequest !== null"
                    [disabled]="!editable()"
                    (change)="useOwnRequest(step)"
                    class="mt-1 size-4 border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span>Its own request</span>
                </label>
              </div>
              @if (step.ownRequest !== null) {
                <label for="wf-own-request" class="sr-only">This step’s own request</label>
                <textarea
                  id="wf-own-request"
                  rows="3"
                  [value]="step.ownRequest"
                  [readOnly]="!editable()"
                  (input)="store.setOwnRequest(step.key, textOf($event))"
                  placeholder="Check the budget justification against the cost table."
                  class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:focus:ring-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                ></textarea>
              }
            </fieldset>
            }

            <fieldset>
              <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Reads</legend>
              <div class="mt-2 space-y-1.5">
                <label class="flex items-center gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input type="checkbox" [checked]="step.reads.includes('request')" [disabled]="!editable()" (change)="toggleRead(step, 'request')" [class]="checkboxClass" />
                  {{ isModelStep(step) ? 'The run’s request' : 'The request' }}
                </label>
                @let filesRequired = step.kind === 'checklist' && step.reads.includes('files');
                <label class="flex items-start gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input
                    type="checkbox"
                    [checked]="step.reads.includes('files')"
                    [disabled]="!editable() || filesRequired"
                    (change)="toggleRead(step, 'files')"
                    [attr.aria-describedby]="step.kind === 'checklist' ? 'wf-files-required' : null"
                    [class]="checkboxClass + ' mt-1'"
                  />
                  <span>
                    The run’s files
                    @if (step.kind === 'checklist') {
                      <span
                        id="wf-files-required"
                        class="block text-xs/5"
                        [class]="step.reads.includes('files') ? 'text-gray-600 dark:text-gray-400' : 'font-medium text-state-warning-700 dark:text-state-warning-300'"
                      >
                        Required: a checklist step fills its sets in from the run’s files.
                      </span>
                    }
                  </span>
                </label>
                @for (other of otherSteps(); track other.key) {
                  @let blocked = readers().has(other.key);
                  <label class="flex items-start gap-3 text-sm/6" [class]="blocked ? 'text-gray-600 dark:text-gray-400' : 'text-gray-900 dark:text-white'">
                    <input
                      type="checkbox"
                      [checked]="step.reads.includes(other.name)"
                      [disabled]="!editable() || blocked"
                      (change)="toggleStepRead(step, other.name)"
                      [attr.aria-describedby]="blocked ? 'wf-read-why-' + other.key : null"
                      [class]="checkboxClass + ' mt-1'"
                    />
                    <span>
                      {{ store.stepLabel(other) }}’s result
                      <span class="block text-xs/5 text-gray-600 dark:text-gray-400" [attr.id]="blocked ? 'wf-read-why-' + other.key : null">
                        @if (blocked) {
                          {{ blockedReason(other.key) }}
                        } @else {
                          Stage {{ store.stageOf(other.key) }}
                        }
                      </span>
                    </span>
                  </label>
                }
              </div>
              <p id="wf-reads-rules" class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400">
                @if (otherSteps().length) {
                  A step runs in the stage after the latest step it reads. It can’t read itself, or a step that reads it.
                } @else {
                  Add another step and this one can read its result.
                }
                {{ store.stepCount() }} of {{ maxSteps }} steps.
              </p>
            </fieldset>

            @if (editable()) {
              <div class="flex flex-wrap items-center gap-2 border-t border-gray-200 pt-4 dark:border-gray-700">
                <button
                  id="wf-move-up"
                  type="button"
                  (click)="move(step, -1)"
                  [disabled]="position().first"
                  [attr.aria-label]="'Move ' + store.stepLabel(step) + ' up'"
                  class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  <ng-icon name="heroArrowUp" class="size-4" aria-hidden="true" />
                  Move up
                </button>
                <button
                  id="wf-move-down"
                  type="button"
                  (click)="move(step, 1)"
                  [disabled]="position().last"
                  [attr.aria-label]="'Move ' + store.stepLabel(step) + ' down'"
                  class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  <ng-icon name="heroArrowDown" class="size-4" aria-hidden="true" />
                  Move down
                </button>
                <button
                  type="button"
                  (click)="remove(step)"
                  class="ml-auto inline-flex items-center gap-1.5 rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 dark:text-state-danger-400 dark:hover:bg-state-danger-900/20"
                >
                  <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                  Remove step
                </button>
              </div>
            }
          </section>
        }
      }
    }
  `,
})
export class WorkflowStepTabComponent {
  protected readonly store = inject(WorkflowDesignerStore);
  private readonly injector = inject(Injector);

  /** The canvas card to focus after this tab removed the step it was showing. */
  readonly focusCanvas = output<string>();

  protected readonly fileChoices = FILE_CHOICES;
  protected readonly checkboxClass =
    'size-4 rounded border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800';
  protected readonly radioClass =
    'mt-1 size-4 border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800';
  protected readonly textareaClass =
    'mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:focus:ring-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400';
  protected readonly inputClass =
    'mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:focus:ring-primary-accessible-dark dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400';
  protected readonly maxSteps = MAX_WORKFLOW_STEPS;
  protected readonly maxSets = MAX_CHECKLIST_FIELD_SETS;
  protected readonly iconButtonClass =
    'rounded-lg p-1 text-gray-600 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus-visible:outline-primary-accessible-dark';
  protected readonly linkButtonClass =
    'rounded-md font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:focus-visible:outline-primary-accessible-dark';
  protected readonly filter = signal('');

  /** The workflow's skill, which holds its field sets; null until the first save makes it. */
  protected readonly skillId = computed(() => this.store.ref()?.skillId ?? null);
  protected readonly skillSets = this.store.skillFieldSets;
  protected readonly skillSetNames = computed(() => {
    const sets = this.skillSets();
    return sets.state === 'ready' ? sets.names : [];
  });
  /** The skill's sets this checklist step doesn't run yet, by name. */
  protected readonly addableSets = computed(() => {
    const chosen = new Set(this.step()?.fieldSets ?? []);
    return this.skillSetNames().filter((name) => !chosen.has(name));
  });

  protected readonly draft = this.store.draft;
  protected readonly editable = this.store.isOwned;

  protected readonly kind = computed<'start' | 'result' | 'step'>(() => {
    const selected = this.store.selected();
    if (selected === START_NODE) return 'start';
    if (selected === RESULT_NODE) return 'result';
    return this.store.selectedStep() ? 'step' : 'start';
  });

  protected readonly step = this.store.selectedStep;

  protected readonly view = computed(() => {
    const step = this.step();
    return step ? this.store.agentFor(step).view : null;
  });

  /** The step's Agent is one the picker lists. */
  protected readonly known = computed(() => {
    const step = this.step();
    return !!step && !!this.store.agentFor(step).mention;
  });

  protected readonly model = computed(() => {
    const step = this.step();
    return step ? this.store.modelLabel(step) : null;
  });

  protected readonly stageNumber = computed(() => {
    const step = this.step();
    if (!step) return 1;
    return this.store.stages().findIndex((keys) => keys.includes(step.key)) + 1;
  });

  protected readonly position = computed(() => {
    const step = this.step();
    const stage = this.store.stages().find((keys) => step && keys.includes(step.key)) ?? [];
    const at = step ? stage.indexOf(step.key) : -1;
    return { first: at <= 0, last: at === stage.length - 1 };
  });

  protected readonly otherSteps = computed(() => {
    const step = this.step();
    return this.draft().steps.filter((other) => other.key !== step?.key);
  });

  /** The steps this one can't read, because each already reads it (directly or on the way). */
  protected readonly readers = computed(() => {
    const step = this.step();
    return step ? this.store.readersOf(step.key) : new Map<string, string | null>();
  });

  protected readonly choices = computed(() => {
    const needle = this.filter().trim().toLowerCase();
    const all = this.store.agents();
    if (!needle) return all;
    return all.filter((agent) => agent.name.toLowerCase().includes(needle) || (agent.subtitle ?? '').toLowerCase().includes(needle));
  });

  /**
   * Why the prompt step's model won't do, or null: none chosen, one this user can't use (the
   * server's word), or a text-only model on a step that reads files.
   */
  protected readonly modelProblem = computed(() => {
    const step = this.step();
    if (!step || !isModelDraftStep(step)) return null;
    if (!step.modelId) return 'Choose the model this step runs on.';
    const listed = this.store.promptModels().find((choice) => choice.modelId === step.modelId);
    if (listed && !listed.readsDocuments && step.reads.includes('files')) {
      return 'This model reads text only. Choose another, or untick the run’s files below.';
    }
    const judged = this.store.modelViewFor(step);
    if (judged && !judged.canRun) return modelReasonCopy(judged.reason);
    if (!listed && this.store.promptModels().length) return 'This step’s model isn’t one you can choose. Choose another.';
    return null;
  });

  protected isModelStep(step: DraftStep): boolean {
    return isModelDraftStep(step);
  }

  protected kindLabel(step: DraftStep): string {
    return step.kind === 'checklist' ? 'Checklist step' : step.kind === 'prompt' ? 'Prompt step' : 'Agent step';
  }

  protected setReason(reason: string | undefined): string {
    return fieldSetReasonCopy(reason);
  }

  protected setControlId(direction: 'up' | 'down' | 'remove', name: string): string {
    return `wf-set-${direction}-${name}`;
  }

  /** Make the workflow's skill, so its field sets can be added; the page takes the new URL. */
  protected async createCarrier(): Promise<void> {
    if (await this.store.createCarrier()) {
      afterNextRender(() => document.querySelector<HTMLElement>('[data-testid="wf-skill-sets"] a, [data-testid="wf-skill-sets"] button')?.focus(), {
        injector: this.injector,
      });
    }
  }

  protected addSet(step: DraftStep, name: string): void {
    this.store.toggleFieldSet(step.key, name);
  }

  /** Remove a set, then put focus on the next set's Remove (or the one before), else on the guidance. */
  protected removeSet(step: DraftStep, name: string): void {
    const at = step.fieldSets.indexOf(name);
    this.store.toggleFieldSet(step.key, name);
    afterNextRender(
      () => {
        const rest = this.step()?.fieldSets ?? [];
        const next = rest[Math.min(at, rest.length - 1)];
        (next ? document.getElementById(this.setControlId('remove', next)) : document.getElementById('wf-guidance'))?.focus();
      },
      { injector: this.injector },
    );
  }

  /** Move a set, keeping focus on the control that moved it, or the other one at an end (§4.4). */
  protected moveSet(step: DraftStep, name: string, delta: -1 | 1): void {
    this.store.moveFieldSet(step.key, name, delta);
    afterNextRender(
      () => {
        const sets = this.step()?.fieldSets ?? [];
        const at = sets.indexOf(name);
        const atEnd = delta < 0 ? at === 0 : at === sets.length - 1;
        const direction = (delta < 0) !== atEnd ? 'up' : 'down';
        document.getElementById(this.setControlId(direction, name))?.focus();
      },
      { injector: this.injector },
    );
  }

  /** A step's name as the author reads it: "budget_check" → "Budget check", as the card says it. */
  protected nameOf(step: DraftStep): string {
    return this.store.stepLabel(step);
  }

  protected rename(step: DraftStep, event: Event): void {
    const label = this.textOf(event).trim();
    if (label) this.store.renameStep(step.key, label);
  }

  /** The instruction is the step's own request; blank, the step is asked the run's. */
  protected setInstruction(step: DraftStep, event: Event): void {
    const text = this.textOf(event);
    this.store.setOwnRequest(step.key, text.trim() ? text : null);
  }

  protected usedElsewhere(agentId: string): boolean {
    const step = this.step();
    return this.draft().steps.some((other) => other.key !== step?.key && other.agentId === agentId);
  }

  /** "reads this step", or "reads Colleague, which reads this step". */
  protected blockedReason(key: string): string {
    const via = this.readers().get(key);
    if (!via) return 'Reads this step, so this step can’t read it';
    const through = this.draft().steps.find((s) => s.name === via);
    return `Reads ${through ? this.store.stepLabel(through) : via}, which reads this step`;
  }

  protected includes(name: string): boolean {
    return this.store.resultSteps().includes(name);
  }

  protected toggleStepRead(step: DraftStep, name: string): void {
    const reads = step.reads.includes(name) ? step.reads.filter((r) => r !== name) : [...step.reads, name];
    this.store.setReads(step.key, reads);
  }

  /** Merge, then the instruction: it is what the merge is for. */
  protected useMerge(): void {
    this.store.setResultMode('merge');
    afterNextRender(() => document.getElementById('wf-result-instruction')?.focus(), { injector: this.injector });
  }

  protected textOf(event: Event): string {
    return (event.target as HTMLInputElement | HTMLTextAreaElement).value;
  }

  protected choose(agent: MentionableAgent): void {
    const step = this.step();
    if (step) this.store.chooseAgent(step.key, agent);
  }

  protected useOwnRequest(step: DraftStep): void {
    this.store.setOwnRequest(step.key, step.ownRequest ?? this.draft().request);
    afterNextRender(() => document.getElementById('wf-own-request')?.focus(), { injector: this.injector });
  }

  protected toggleRead(step: DraftStep, read: 'request' | 'files'): void {
    const reads = step.reads.includes(read) ? step.reads.filter((r) => r !== read) : [...step.reads, read];
    this.store.setReads(step.key, reads);
  }

  /**
   * Move, keeping focus on the control that moved it (§4.4). At an end that button is disabled,
   * so focus goes to the other direction's, as the field-set editor does.
   */
  protected move(step: DraftStep, delta: -1 | 1): void {
    this.store.move(step.key, delta);
    afterNextRender(
      () => {
        const { first, last } = this.position();
        const atEnd = delta < 0 ? first : last;
        const id = (delta < 0) !== atEnd ? 'wf-move-up' : 'wf-move-down';
        document.getElementById(id)?.focus();
      },
      { injector: this.injector },
    );
  }

  protected remove(step: DraftStep): void {
    const next = this.store.removeStep(step.key);
    this.focusCanvas.emit(next);
  }
}
