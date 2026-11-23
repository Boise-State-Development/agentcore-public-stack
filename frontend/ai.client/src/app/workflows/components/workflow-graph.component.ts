import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  afterRenderEffect,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowsPointingIn,
  heroArrowsPointingOut,
  heroCheckCircle,
  heroExclamationCircle,
  heroExclamationTriangle,
  heroInboxArrowDown,
  heroMinusCircle,
  heroPaperClip,
  heroPauseCircle,
  heroPlus,
  heroUserPlus,
} from '@ng-icons/heroicons/outline';

import { AgentIconComponent } from '../../agents/components/agent-icon.component';
import { ChecklistStepIconComponent } from './checklist-step-icon.component';
import { PromptStepIconComponent } from './prompt-step-icon.component';
import { RunStepState } from '../../session/services/handoff/workflow-run-progress';

/** One step's card as the graph draws it. Everything it says is in text, never colour alone. */
export interface GraphStepCard {
  key: string;
  /**
   * `prompt` for a prompt step (workflow-designer §21.3), `checklist` for a checklist step (§22.3),
   * each drawn with its own mark; absent for an Agent step.
   */
  kind?: 'prompt' | 'checklist';
  /** The Agent's name, "Choose an Agent", or a model step's own name. */
  title: string;
  /** "Agent · Sonnet 5.5". */
  meta: string;
  agentId: string | null;
  emoji?: string;
  iconUrl?: string;
  /** Why this step can't run as it is ("Choose an Agent", "You can't use this Agent"). */
  warning?: string;
  /** "reads the request and the files": the card's edges, in words (§4.4). */
  readsLabel: string;
}

/**
 * A step's status in run mode (§7 D4): the state its line on the run's row has (`runProgress` /
 * `stepState`), that line's phrase, and how long it took once it has finished.
 */
export interface GraphStepStatus {
  state: RunStepState;
  /** "Waiting for your approval to run budget_check", worded as the run's row words it. */
  phrase: string;
  /** "1m 04s", once the step has finished. */
  elapsed?: string;
}

/** What the canvas draws: the designer's editable graph, or a run's status on the same graph. */
export type GraphMode = 'design' | 'run';

/** The two fixed cards: Starts with and Result. */
export interface GraphFixedCard {
  title: string;
  meta: string;
}

/** An edge between two node ids: `start`, `result`, or a step's key. Drawn, never announced. */
export type GraphEdge = readonly [from: string, to: string];

/** A node's selector. Ids are `start`, `result`, `add` or a step key, never a quote. */
function nodeSelector(id: string): string {
  return `[data-node-id="${id.replace(/["\\]/g, '')}"]`;
}

/** Fit never scales below this: under it a card can't be read, so the canvas scrolls (§4.2). */
export const MIN_FIT_SCALE = 0.6;

/** The scale Fit gives a graph of `graph` size in a canvas of `canvas` size. */
export function fitScale(graph: { width: number; height: number }, canvas: { width: number; height: number }): number {
  if (graph.width <= 0 || graph.height <= 0 || canvas.width <= 0 || canvas.height <= 0) return 1;
  const scale = Math.min(canvas.width / graph.width, canvas.height / graph.height);
  return Math.min(1, Math.max(MIN_FIT_SCALE, scale));
}

/**
 * The workflow graph, in design mode (docs/specs/workflow-designer.md §4, §7 D4).
 *
 * It arranges itself: Starts with, then one column per stage (a list of steps, authored order),
 * then Result. Nothing is placed by hand and nothing is stored about where a card is.
 *
 * For assistive technology it is what it is in the DOM: a labelled group per column, each a list
 * of buttons whose names carry what the picture shows. The connectors are an SVG drawn from the
 * cards' measured boxes and hidden (`aria-hidden`), since every edge is said on the card it ends
 * at. Below `md` the columns stack as headed sections and no connector is drawn.
 *
 * In design mode each stage ends with Add step (`add` emits the stage's 0-based index), and a
 * last column starts the next stage (`add` emits one past the last). A step added to stage N
 * reads every step of stage N-1, which is what puts it there (PR-D.5b-1).
 *
 * Moving around (§4.2): native scrolling in a focusable region, a drag on the empty grid pans it,
 * and Fit scales the whole graph to the canvas, never below 60%. No wheel or pinch zoom.
 *
 * The canvas is the page's one bold surface: the graph-paper grid and two still blobs in the
 * brand colour, with frosted cards on top.
 *
 * Run mode (§7 D4, PR-D.4) is the same graph with a run's status on it: each step card shows its
 * status mark and phrase from the run's row (`status`), and its elapsed time once finished; a
 * connector is drawn in the brand colour once the node it leaves has finished (`finished`); Add
 * step is hidden and the cards are not buttons, since nothing on the canvas edits a run. Every
 * state is said in text, never by colour alone, and reduced motion stops the running pulse. The
 * columns come from `stages` either way, so a run with several stages draws as it will once D.5b
 * gives the canvas them.
 *
 * Its inputs only ever grow (additive): the run's row and the review sheet embed it too.
 */
@Component({
  selector: 'app-workflow-graph',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, NgTemplateOutlet, AgentIconComponent, PromptStepIconComponent, ChecklistStepIconComponent],
  providers: [
    provideIcons({
      heroArrowsPointingIn,
      heroArrowsPointingOut,
      heroCheckCircle,
      heroExclamationCircle,
      heroMinusCircle,
      heroPauseCircle,
      heroExclamationTriangle,
      heroInboxArrowDown,
      heroPaperClip,
      heroPlus,
      heroUserPlus,
    }),
  ],
  host: { class: 'relative block min-h-0 bg-gray-50 md:overflow-hidden dark:bg-gray-900' },
  styles: [
    `
      .wf-grid {
        background-image:
          linear-gradient(color-mix(in srgb, var(--color-primary-500) 8%, transparent) 1px, transparent 1px),
          linear-gradient(90deg, color-mix(in srgb, var(--color-primary-500) 8%, transparent) 1px, transparent 1px);
        background-size: 24px 24px;
        background-attachment: local;
      }
      :host-context(html.dark) .wf-grid {
        background-image:
          linear-gradient(color-mix(in srgb, var(--color-primary-300) 8%, transparent) 1px, transparent 1px),
          linear-gradient(90deg, color-mix(in srgb, var(--color-primary-300) 8%, transparent) 1px, transparent 1px);
      }
      .wf-blob {
        border-radius: 42% 58% 61% 39% / 46% 41% 59% 54%;
      }
    `,
  ],
  template: `
    <!-- The two still blobs. Decorative, so out of the tree and out of the pointer's way. -->
    <div class="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
      <div class="wf-blob absolute left-[8%] top-[10%] h-80 w-[26rem] bg-primary-500/15 blur-3xl dark:bg-primary-400/20"></div>
      <div class="wf-blob absolute bottom-[6%] right-[6%] h-72 w-[22rem] bg-primary-300/15 blur-3xl dark:bg-primary-300/10"></div>
    </div>

    <div
      #scroller
      class="wf-grid relative md:h-full md:overflow-auto focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark md:cursor-grab"
      [style.cursor]="panning() ? 'grabbing' : null"
      [class.select-none]="panning()"
      tabindex="0"
      role="region"
      [attr.aria-label]="regionLabel()"
      (pointerdown)="onPointerDown($event)"
      (pointermove)="onPointerMove($event)"
      (pointerup)="endPan($event)"
      (pointercancel)="endPan($event)"
    >
      <div class="md:inline-block" [style.width.px]="frame().width" [style.height.px]="frame().height">
        <div
          #graph
          class="relative flex w-full flex-col gap-8 p-4 sm:p-6 md:inline-flex md:w-auto md:flex-row md:items-center md:gap-14 md:origin-top-left md:px-8 md:py-16"
          [style.transform]="transform()"
        >
          <svg class="pointer-events-none absolute inset-0 hidden size-full overflow-visible md:block" aria-hidden="true">
            @for (path of paths(); track path.id) {
              <path
                [attr.d]="path.d"
                [attr.data-edge]="path.id"
                [attr.data-finished]="path.finished ? '' : null"
                fill="none"
                [class]="path.finished ? edgeFinishedClass : edgeClass"
                [attr.stroke-width]="path.finished ? 2 : 1.5"
              />
            }
          </svg>

          <!-- Starts with -->
          <section class="relative md:w-56" [attr.aria-labelledby]="ids.start">
            <h2 [id]="ids.start" class="mb-1.5 text-xs/5 font-medium text-gray-600 dark:text-gray-300">Starts with</h2>
            @if (mode() === 'run') {
              <div data-node-id="start" [class]="runCardClass">
                <ng-container *ngTemplateOutlet="startBody" />
              </div>
            } @else {
              <button
                type="button"
                data-node-id="start"
                [attr.aria-pressed]="selected() === 'start'"
                [attr.aria-label]="'Starts with: ' + start().meta"
                (click)="selectNode.emit('start')"
                [class]="cardClass"
              >
                <ng-container *ngTemplateOutlet="startBody" />
              </button>
            }
            <ng-template #startBody>
              <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50" aria-hidden="true">
                <ng-icon name="heroPaperClip" class="size-4.5" />
              </span>
              <span class="flex min-w-0 flex-col">
                <span class="truncate text-sm/5 font-medium text-gray-900 dark:text-white">{{ start().title }}</span>
                <span class="truncate text-xs/5 text-gray-600 dark:text-gray-300">{{ start().meta }}</span>
              </span>
            </ng-template>
          </section>

          @for (stage of stages(); track $index; let s = $index) {
            <section class="relative md:w-60" role="group" [attr.aria-labelledby]="ids.stage + s">
              <h2 [id]="ids.stage + s" class="mb-1.5 text-xs/5 font-medium text-gray-600 dark:text-gray-300">
                Stage {{ s + 1 }}@if (agentStepCount(stage) > concurrency) { · {{ concurrency }} at once }
              </h2>
              <ul role="list" class="flex flex-col gap-2.5">
                @for (card of stage; track card.key) {
                  <li>
                    @if (mode() === 'run') {
                      @let st = statusOf(card.key);
                      <div [attr.data-node-id]="card.key" [attr.data-state]="st.state" [class]="runCardClass">
                        <span class="relative shrink-0">
                          @if (card.kind === 'prompt') {
                            <app-prompt-step-icon [size]="28" />
                          } @else if (card.kind === 'checklist') {
                            <app-checklist-step-icon [size]="28" />
                          } @else if (card.agentId) {
                            <app-agent-icon [agentId]="card.agentId" [iconUrl]="card.iconUrl" [emoji]="card.emoji" [size]="28" />
                          }
                          <span class="absolute -right-1.5 -bottom-1.5 rounded-full bg-white dark:bg-gray-800" aria-hidden="true">
                            <ng-container *ngTemplateOutlet="mark; context: { $implicit: st.state }" />
                          </span>
                        </span>
                        <span class="flex min-w-0 flex-col">
                          <span class="truncate text-sm/5 font-medium text-gray-900 dark:text-white">{{ card.title }}</span>
                          <span class="line-clamp-2 text-xs/5" [class]="phraseClass(st.state)">
                            {{ st.phrase }}@if (st.elapsed) {<span class="tabular-nums"> · {{ st.elapsed }}</span>}
                          </span>
                        </span>
                      </div>
                    } @else {
                    <button
                      type="button"
                      [attr.data-node-id]="card.key"
                      [attr.aria-pressed]="selected() === card.key"
                      [attr.aria-label]="accessibleName(card, s)"
                      (click)="selectNode.emit(card.key)"
                      [class]="cardClass"
                    >
                      @if (card.kind === 'prompt') {
                        <app-prompt-step-icon [size]="28" />
                      } @else if (card.kind === 'checklist') {
                        <app-checklist-step-icon [size]="28" />
                      } @else if (card.agentId) {
                        <app-agent-icon [agentId]="card.agentId" [iconUrl]="card.iconUrl" [emoji]="card.emoji" [size]="28" />
                      } @else {
                        <span class="grid size-7 shrink-0 place-items-center rounded-lg border border-dashed border-gray-400 text-gray-600 dark:border-gray-500 dark:text-gray-300" aria-hidden="true">
                          <ng-icon name="heroUserPlus" class="size-4" />
                        </span>
                      }
                      <span class="flex min-w-0 flex-col">
                        <span class="truncate text-sm/5 font-medium text-gray-900 dark:text-white">{{ card.title }}</span>
                        @if (card.warning) {
                          <span class="flex items-center gap-1 truncate text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
                            <ng-icon name="heroExclamationTriangle" class="size-3.5 shrink-0" aria-hidden="true" />
                            {{ card.warning }}
                          </span>
                        } @else {
                          <span class="truncate text-xs/5 text-gray-600 dark:text-gray-300">{{ card.meta }}</span>
                        }
                      </span>
                    </button>
                    }
                  </li>
                }
              </ul>
              @if (mode() === 'design') {
                <button
                  type="button"
                  [attr.data-node-id]="addNodeId(s)"
                  (click)="add.emit(s)"
                  [disabled]="stepCount() >= maxSteps()"
                  [attr.aria-label]="'Add step to stage ' + (s + 1)"
                  [class]="addClass"
                  [attr.aria-describedby]="ids.cap"
                >
                  <ng-icon name="heroPlus" class="size-3.5" aria-hidden="true" />
                  Add step
                </button>
                @if (s === 0) {
                  <p [id]="ids.cap" class="mt-1 text-center text-xs/5 text-gray-600 tabular-nums dark:text-gray-300">
                    {{ stepCount() }} of {{ maxSteps() }} steps
                  </p>
                }
              }
            </section>
          }

          <!-- The next stage: not a stage yet, only a way to start one (§4.1). -->
          @if (mode() === 'design' && stepCount() > 0) {
            <section class="relative md:w-60" role="group" [attr.aria-labelledby]="ids.next">
              <h2 [id]="ids.next" class="mb-1.5 text-xs/5 font-medium text-gray-600 dark:text-gray-300">Stage {{ stages().length + 1 }}</h2>
              <!-- Frosted like a card, so a connector passing behind it to Result stays behind it. -->
              <button
                type="button"
                [attr.data-node-id]="addNodeId(stages().length)"
                (click)="add.emit(stages().length)"
                [disabled]="stepCount() >= maxSteps()"
                [attr.aria-label]="'Add step to stage ' + (stages().length + 1) + ', a new stage'"
                [class]="newStageClass"
                [attr.aria-describedby]="ids.nextHelp + ' ' + ids.cap"
              >
                <span class="flex items-center gap-1.5 text-xs/5 font-medium text-gray-700 dark:text-gray-200">
                  <ng-icon name="heroPlus" class="size-3.5" aria-hidden="true" />
                  Add step
                </span>
                <span [id]="ids.nextHelp" class="text-xs/5 font-normal text-gray-600 dark:text-gray-300">
                  Runs after stage {{ stages().length }} and reads its results
                </span>
              </button>
            </section>
          }

          <!-- Result -->
          <section class="relative md:w-56" [attr.aria-labelledby]="ids.result">
            <h2 [id]="ids.result" class="mb-1.5 text-xs/5 font-medium text-gray-600 dark:text-gray-300">Result</h2>
            @if (mode() === 'run') {
              <div data-node-id="result" [class]="runCardClass">
                <ng-container *ngTemplateOutlet="resultBody" />
              </div>
            } @else {
              <button
                type="button"
                data-node-id="result"
                [attr.aria-pressed]="selected() === 'result'"
                [attr.aria-label]="'Result: ' + result().title + ', ' + result().meta"
                (click)="selectNode.emit('result')"
                [class]="cardClass"
              >
                <ng-container *ngTemplateOutlet="resultBody" />
              </button>
            }
            <ng-template #resultBody>
              <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50" aria-hidden="true">
                <ng-icon name="heroInboxArrowDown" class="size-4.5" />
              </span>
              <span class="flex min-w-0 flex-col">
                <span class="truncate text-sm/5 font-medium text-gray-900 dark:text-white">{{ result().title }}</span>
                <span class="truncate text-xs/5 text-gray-600 dark:text-gray-300">{{ result().meta }}</span>
              </span>
            </ng-template>
          </section>
        </div>
      </div>
    </div>

    <!-- Fit: two states, actual size and Fit (§4.2). Only where the graph is a canvas, not a stack. -->
    <div class="pointer-events-none absolute bottom-4 right-4 hidden md:block">
      <button
        type="button"
        (click)="toggleFit()"
        [attr.aria-pressed]="fit()"
        class="pointer-events-auto inline-flex items-center gap-1.5 rounded-2xl border border-gray-500 bg-white px-3 py-1.5 text-xs/5 font-medium text-gray-700 shadow-xs hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-400 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
      >
        <ng-icon [name]="fit() ? 'heroArrowsPointingOut' : 'heroArrowsPointingIn'" class="size-4" aria-hidden="true" />
        Fit
        @if (fit() && scale() < 1) {
          <span class="tabular-nums text-gray-600 dark:text-gray-300">{{ percent() }}%</span>
        }
      </button>
    </div>

    <!-- The run row's status marks (workflow-runs §20.2), never the only sign of a state. -->
    <ng-template #mark let-state>
      <span class="flex size-4.5 items-center justify-center">
        @switch (state) {
          @case ('running') {
            <span class="relative flex size-2.5" data-mark="running">
              <span class="absolute inline-flex size-full animate-ping rounded-full bg-primary-500 opacity-60 motion-reduce:animate-none dark:bg-primary-300"></span>
              <span class="relative inline-flex size-2.5 rounded-full bg-primary-500 dark:bg-primary-300"></span>
            </span>
          }
          @case ('needs_input') {
            <ng-icon name="heroExclamationCircle" class="size-4.5 text-state-warning-700 dark:text-state-warning-400" />
          }
          @case ('done') {
            <ng-icon name="heroCheckCircle" class="size-4.5 text-state-success-600 dark:text-state-success-400" />
          }
          @case ('queued') {
            <span class="size-2.5 rounded-full ring-1 ring-inset ring-gray-500"></span>
          }
          @case ('not_started') {
            <ng-icon name="heroPauseCircle" class="size-4.5 text-gray-500 dark:text-gray-400" />
          }
          @default {
            <ng-icon name="heroMinusCircle" class="size-4.5 text-gray-500 dark:text-gray-400" />
          }
        }
      </span>
    </ng-template>
  `,
})
export class WorkflowGraphComponent {
  readonly stages = input.required<GraphStepCard[][]>();
  readonly edges = input.required<readonly GraphEdge[]>();
  readonly start = input.required<GraphFixedCard>();
  readonly result = input.required<GraphFixedCard>();
  readonly selected = input<string | null>(null);
  readonly stepCount = input.required<number>();
  readonly maxSteps = input.required<number>();
  readonly concurrency = 4;

  /**
   * The cards that take one of the run's `concurrency` slots: Agent steps. A prompt or checklist step
   * takes none (§21.3, §22.3): a run's ready model steps go together on one invocation, so a stage of
   * five prompt steps does not run "4 at once".
   */
  protected agentStepCount(stage: readonly GraphStepCard[]): number {
    return stage.filter((card) => !card.kind).length;
  }
  /** `run` draws a run's status on the graph (§7 D4); `design` is the editable canvas. */
  readonly mode = input<GraphMode>('design');
  /** Run mode: each step card's status, by step key. */
  readonly status = input<ReadonlyMap<string, GraphStepStatus>>(new Map());
  /** Run mode: the nodes that have finished. A connector leaving one is drawn in the brand colour. */
  readonly finished = input<ReadonlySet<string>>(new Set());
  /** The scroll region's name, for a host that shows more than one graph or a graph of its own kind. */
  readonly label = input<string | null>(null);

  readonly selectNode = output<string>();
  readonly add = output<number>();

  private static nextId = 0;
  /**
   * Ids unique to this instance, so two graphs on one page (two runs' rows, or a row and the sheet's
   * small graph) never point `aria-labelledby` at the other's heading.
   */
  protected readonly ids = (() => {
    const p = `wf-graph-${WorkflowGraphComponent.nextId++}`;
    return { start: `${p}-col-start`, stage: `${p}-col-stage-`, next: `${p}-col-next`, result: `${p}-col-result`, cap: `${p}-step-cap`, nextHelp: `${p}-next-help` };
  })();

  protected readonly regionLabel = computed(() => this.label() ?? (this.mode() === 'run' ? 'Test run canvas' : 'Workflow canvas'));

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly scroller = viewChild.required<ElementRef<HTMLElement>>('scroller');
  private readonly graph = viewChild.required<ElementRef<HTMLElement>>('graph');

  protected readonly cardClass =
    'flex w-full items-center gap-2.5 rounded-2xl border border-gray-200 bg-white/80 px-3 py-2.5 text-left shadow-xs backdrop-blur-md transition-colors hover:border-gray-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark motion-reduce:transition-none aria-pressed:border-primary-accessible aria-pressed:ring-1 aria-pressed:ring-primary-accessible dark:border-gray-600 dark:bg-gray-800/75 dark:hover:border-gray-500 dark:aria-pressed:border-primary-300 dark:aria-pressed:ring-primary-300';

  /** A run card: the design card's frosted look, with nothing to press. */
  protected readonly runCardClass =
    'flex w-full items-center gap-2.5 rounded-2xl border border-gray-200 bg-white/80 px-3 py-2.5 text-left shadow-xs backdrop-blur-md dark:border-gray-600 dark:bg-gray-800/75';
  protected readonly addClass =
    'mt-2.5 flex w-full items-center justify-center gap-1.5 rounded-2xl border border-dashed border-gray-500 bg-white/40 px-3 py-1.5 text-xs/5 font-medium text-gray-700 transition-colors hover:bg-white/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark disabled:cursor-not-allowed disabled:opacity-60 motion-reduce:transition-none dark:border-gray-400 dark:bg-gray-800/40 dark:text-gray-200 dark:hover:bg-gray-800/80';
  protected readonly newStageClass =
    'flex w-full flex-col items-center gap-0.5 rounded-2xl border border-dashed border-gray-500 bg-white/80 px-3 py-2 text-center shadow-xs backdrop-blur-md transition-colors hover:bg-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark disabled:cursor-not-allowed disabled:opacity-60 motion-reduce:transition-none dark:border-gray-400 dark:bg-gray-800/75 dark:hover:bg-gray-800';
  protected readonly edgeClass =
    'stroke-gray-500 transition-[stroke] duration-300 motion-reduce:transition-none dark:stroke-gray-400';
  protected readonly edgeFinishedClass =
    'stroke-primary-500 transition-[stroke] duration-300 motion-reduce:transition-none dark:stroke-primary-300';

  protected readonly fit = signal(false);
  protected readonly panning = signal(false);
  private readonly natural = signal({ width: 0, height: 0 });
  private readonly canvas = signal({ width: 0, height: 0 });
  /** Whether the graph is laid out as a row (md and up); a stack is never scaled. */
  private readonly wide = signal(false);
  protected readonly paths = signal<{ id: string; d: string; finished: boolean }[]>([]);

  protected readonly scale = computed(() => (this.fit() && this.wide() ? fitScale(this.natural(), this.canvas()) : 1));
  protected readonly percent = computed(() => Math.round(this.scale() * 100));
  protected readonly transform = computed(() => (this.scale() < 1 ? `scale(${this.scale()})` : null));
  /** The scaled graph's box, so the scroll region scrolls exactly what is drawn. */
  protected readonly frame = computed(() => {
    if (!this.wide() || this.scale() >= 1) return { width: null, height: null };
    const { width, height } = this.natural();
    return { width: Math.ceil(width * this.scale()), height: Math.ceil(height * this.scale()) };
  });

  private pan: { id: number; x: number; y: number; left: number; top: number } | null = null;

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      if (typeof ResizeObserver === 'undefined') {
        this.measure();
        return;
      }
      const observer = new ResizeObserver(() => this.measure());
      observer.observe(this.scroller().nativeElement);
      observer.observe(this.graph().nativeElement);
      destroyRef.onDestroy(() => observer.disconnect());
      this.measure();
    });
    // Redraw the connectors whenever the cards may have moved.
    afterRenderEffect(() => {
      this.stages();
      this.edges();
      this.selected();
      this.mode();
      this.status();
      this.finished();
      this.drawEdges();
    });
  }

  /** Move focus to a card (or `add`) after the graph redraws, so focus stays with what moved. */
  focusNode(id: string): void {
    const el = this.host.nativeElement.querySelector<HTMLElement>(nodeSelector(id));
    el?.focus();
  }

  protected toggleFit(): void {
    this.fit.update((on) => !on);
  }

  /** Stage 1's Add step is `add`, as it always was; a later stage's is `add-<n>`. */
  protected addNodeId(stage: number): string {
    return stage === 0 ? 'add' : `add-${stage + 1}`;
  }

  /** A step's status in run mode; a step the run doesn't have reads "Not in this run". */
  protected statusOf(key: string): GraphStepStatus {
    return this.status().get(key) ?? { state: 'stopped', phrase: 'Not in this run' };
  }

  /** A step waiting on the user says so in the warning tone, in words as well as colour. */
  protected phraseClass(state: RunStepState): string {
    return state === 'needs_input'
      ? 'font-medium text-state-warning-700 dark:text-state-warning-300'
      : 'text-gray-600 dark:text-gray-300';
  }

  protected accessibleName(card: GraphStepCard, stageIndex: number): string {
    // A model step's meta names its kind and model ("Checklist · Haiku 5.5 · 4 field sets"), which its title doesn't.
    const kind = card.kind ? card.meta : 'Agent step';
    const parts = [card.title, kind, `stage ${stageIndex + 1}`, card.readsLabel];
    if (card.warning) parts.push(card.warning);
    return parts.join(', ');
  }

  protected onPointerDown(event: PointerEvent): void {
    if (event.pointerType !== 'mouse' || event.button !== 0 || !this.wide()) return;
    const target = event.target as Element | null;
    if (target?.closest('button, a, input, textarea, select, label')) return;
    const el = this.scroller().nativeElement;
    this.pan = { id: event.pointerId, x: event.clientX, y: event.clientY, left: el.scrollLeft, top: el.scrollTop };
    el.setPointerCapture?.(event.pointerId);
    this.panning.set(true);
  }

  protected onPointerMove(event: PointerEvent): void {
    if (!this.pan || event.pointerId !== this.pan.id) return;
    const el = this.scroller().nativeElement;
    el.scrollLeft = this.pan.left - (event.clientX - this.pan.x);
    el.scrollTop = this.pan.top - (event.clientY - this.pan.y);
  }

  protected endPan(event: PointerEvent): void {
    if (!this.pan || event.pointerId !== this.pan.id) return;
    this.scroller().nativeElement.releasePointerCapture?.(event.pointerId);
    this.pan = null;
    this.panning.set(false);
  }

  private measure(): void {
    const graph = this.graph().nativeElement;
    const scroller = this.scroller().nativeElement;
    this.wide.set(getComputedStyle(graph).flexDirection === 'row');
    // offsetWidth ignores the transform, so this is the graph at actual size.
    this.natural.set({ width: graph.offsetWidth, height: graph.offsetHeight });
    this.canvas.set({ width: scroller.clientWidth, height: scroller.clientHeight });
    this.drawEdges();
  }

  private drawEdges(): void {
    const graph = this.graph()?.nativeElement;
    if (!graph || getComputedStyle(graph).flexDirection !== 'row') {
      if (this.paths().length) this.paths.set([]);
      return;
    }
    const origin = graph.getBoundingClientRect();
    const scale = this.scale() || 1;
    const box = (id: string) => {
      const el = graph.querySelector<HTMLElement>(nodeSelector(id));
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        left: (r.left - origin.left) / scale,
        right: (r.right - origin.left) / scale,
        mid: (r.top + r.height / 2 - origin.top) / scale,
      };
    };
    const paths: { id: string; d: string; finished: boolean }[] = [];
    const finished = this.mode() === 'run' ? this.finished() : new Set<string>();
    for (const [from, to] of this.edges()) {
      const a = box(from);
      const b = box(to);
      if (!a || !b) continue;
      const x1 = a.right;
      const x2 = b.left;
      const bend = Math.max(24, (x2 - x1) / 2);
      paths.push({
        id: `${from}>${to}`,
        d: `M ${x1} ${a.mid} C ${x1 + bend} ${a.mid}, ${x2 - bend} ${b.mid}, ${x2} ${b.mid}`,
        finished: finished.has(from),
      });
    }
    const current = this.paths();
    if (
      current.length !== paths.length ||
      current.some((p, i) => p.d !== paths[i].d || p.id !== paths[i].id || p.finished !== paths[i].finished)
    ) {
      this.paths.set(paths);
    }
  }
}
