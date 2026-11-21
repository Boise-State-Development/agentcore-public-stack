import { ChangeDetectionStrategy, Component, Injector, afterNextRender, computed, inject, output, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowDown, heroArrowUp, heroExclamationTriangle, heroMagnifyingGlass, heroTrash } from '@ng-icons/heroicons/outline';

import { AgentIconComponent } from '../../agents/components/agent-icon.component';
import { MentionableAgent } from '../../agents/services/agent-mention.service';
import { DraftStep, stepReads } from '../workflow-draft';
import { RESULT_NODE, START_NODE, WorkflowDesignerStore } from '../workflow-designer.store';
import { WorkflowFiles } from '../workflow.models';

const FILE_CHOICES: { value: WorkflowFiles; label: string; hint: string }[] = [
  { value: 'none', label: 'No files', hint: 'The starter only writes a request.' },
  { value: 'one', label: 'Exactly one file', hint: 'The starter attaches one, such as the proposal.' },
  { value: 'any', label: 'Any files', hint: 'None, one or several.' },
];

/**
 * The Step tab (docs/specs/workflow-designer.md §5): edits the selected card. Agent steps and the
 * two fixed cards only; prompt and checklist steps are D.6 and D.7.
 *
 * A dense form, so it stays plain: the canvas is the page's one bold surface.
 */
@Component({
  selector: 'app-workflow-step-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentIconComponent],
  providers: [provideIcons({ heroArrowDown, heroArrowUp, heroExclamationTriangle, heroMagnifyingGlass, heroTrash })],
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
                <input type="radio" name="wf-result" value="card" checked disabled class="mt-1 size-4 border-gray-300 text-primary-accessible dark:border-gray-600 dark:bg-gray-800" />
                <span>
                  One card on the composer
                  <span class="block text-xs/5 text-gray-600 dark:text-gray-400">Every step’s result, sent with your next message.</span>
                </span>
              </label>
              <label class="flex items-start gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
                <input type="radio" name="wf-result" value="merge" disabled class="mt-1 size-4 border-gray-300 dark:border-gray-600 dark:bg-gray-800" />
                <span>
                  A merge that starts by itself
                  <span class="block text-xs/5 text-gray-600 dark:text-gray-400">Not available yet: it comes with stages.</span>
                </span>
              </label>
            </div>
          </fieldset>
          <div>
            <h3 class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">It includes</h3>
            @if (draft().steps.length) {
              <ol class="mt-1 list-decimal space-y-0.5 pl-5 text-sm/6 text-gray-900 dark:text-white">
                @for (step of draft().steps; track step.key) {
                  <li>{{ store.stepLabel(step) }}</li>
                }
              </ol>
              <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">Every step’s result, in the order the steps are listed.</p>
            } @else {
              <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">Each step’s result, once you add steps.</p>
            }
          </div>
        </section>
      }

      @case ('step') {
        @if (step(); as step) {
          <section class="space-y-6" aria-labelledby="wf-step-heading">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <h2 id="wf-step-heading" class="truncate text-base/7 font-semibold text-gray-900 dark:text-white">{{ store.stepLabel(step) }}</h2>
                <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                  Agent step · stage {{ stageNumber() }}@if (model()) { · {{ model() }} }
                </p>
              </div>
            </div>

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

            <fieldset>
              <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Reads</legend>
              <div class="mt-2 space-y-1.5">
                <label class="flex items-center gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input type="checkbox" [checked]="step.reads.includes('request')" [disabled]="!editable()" (change)="toggleRead(step, 'request')" [class]="checkboxClass" />
                  The request
                </label>
                <label class="flex items-center gap-3 text-sm/6 text-gray-900 dark:text-white">
                  <input type="checkbox" [checked]="step.reads.includes('files')" [disabled]="!editable()" (change)="toggleRead(step, 'files')" [class]="checkboxClass" />
                  The run’s files
                </label>
                @for (other of otherSteps(); track other.key) {
                  <label class="flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
                    <input type="checkbox" [checked]="step.reads.includes(other.name)" disabled [class]="checkboxClass" [attr.aria-describedby]="'wf-reads-stages'" />
                    {{ store.stepLabel(other) }}’s result
                  </label>
                }
              </div>
              @if (otherSteps().length) {
                <p id="wf-reads-stages" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                  Reading another step’s result needs stages, which come later.
                </p>
              }
              @if (stepReadsAny(step)) {
                <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">This step reads another step’s result in the saved file.</p>
              }
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
  protected readonly filter = signal('');

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

  protected readonly choices = computed(() => {
    const needle = this.filter().trim().toLowerCase();
    const all = this.store.agents();
    if (!needle) return all;
    return all.filter((agent) => agent.name.toLowerCase().includes(needle) || (agent.subtitle ?? '').toLowerCase().includes(needle));
  });

  protected usedElsewhere(agentId: string): boolean {
    const step = this.step();
    return this.draft().steps.some((other) => other.key !== step?.key && other.agentId === agentId);
  }

  protected stepReadsAny(step: DraftStep): boolean {
    return stepReads(step).length > 0;
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
