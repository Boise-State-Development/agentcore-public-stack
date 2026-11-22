import { Dialog } from '@angular/cdk/dialog';
import { Location } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCheck,
  heroCheckCircle,
  heroExclamationTriangle,
  heroPencilSquare,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';

import { ConfirmationDialogComponent, ConfirmationDialogData } from '../components/confirmation-dialog';
import { RunStepLine, fieldSetsLabel } from '../session/services/handoff/workflow-run-progress';
import { WorkflowRunView } from '../session/services/handoff/workflow-run.models';
import {
  GraphEdge,
  GraphMode,
  GraphStepCard,
  GraphStepStatus,
  WorkflowGraphComponent,
} from './components/workflow-graph.component';
import { WorkflowPreviewTabComponent } from './components/workflow-preview-tab.component';
import { WorkflowStepTabComponent } from './components/workflow-step-tab.component';
import { WorkflowTestRunPanelComponent } from './components/workflow-test-run-panel.component';
import { DraftStep, stepReads } from './workflow-draft';
import { RESULT_NODE, START_NODE, WorkflowDesignerStore } from './workflow-designer.store';
import { WorkflowTestRunService, isStartedRun } from './workflow-test-run.service';
import { MAX_WORKFLOW_STEPS, WorkflowFiles } from './workflow.models';

type PanelTab = 'step' | 'preview' | 'test';

const START_META: Record<WorkflowFiles, string> = {
  none: 'A request, no files',
  one: 'A request and one file',
  any: 'A request and any files',
};

/**
 * The workflow designer (docs/specs/workflow-designer.md §5): the Agent Designer's layout, a
 * full-screen page with the canvas taking the spare width and a two-tab panel beside it.
 *
 * `/workflows/new` (optionally `?template=proposal-review`) edits a draft that nothing has stored:
 * Save creates the carrying skill and writes the file in one go, then the URL becomes the
 * workflow's own without a navigation, so the page and its selection stay put.
 * `/workflows/:skillId/:name` opens a saved one.
 */
@Component({
  selector: 'app-workflow-designer-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    RouterLink,
    WorkflowGraphComponent,
    WorkflowStepTabComponent,
    WorkflowPreviewTabComponent,
    WorkflowTestRunPanelComponent,
  ],
  providers: [
    WorkflowDesignerStore,
    provideIcons({ heroArrowLeft, heroCheck, heroCheckCircle, heroExclamationTriangle, heroPencilSquare, heroTrash }),
  ],
  templateUrl: './workflow-designer.page.html',
})
export class WorkflowDesignerPage {
  protected readonly store = inject(WorkflowDesignerStore);
  private readonly router = inject(Router);
  private readonly location = inject(Location);
  private readonly dialog = inject(Dialog);
  private readonly injector = inject(Injector);
  private readonly testRuns = inject(WorkflowTestRunService);

  /** Route params, bound by `withComponentInputBinding()`. Absent on `/workflows/new`. */
  readonly skillId = input<string>();
  readonly name = input<string>();
  readonly template = input<string>();

  private readonly graph = viewChild(WorkflowGraphComponent);

  protected readonly tab = signal<PanelTab>('step');
  protected readonly maxSteps = MAX_WORKFLOW_STEPS;

  constructor() {
    effect(() => {
      const skillId = this.skillId();
      const name = this.name();
      const template = this.template() ?? null;
      untracked(() => {
        if (skillId && name) void this.store.open({ skillId, name });
        else this.store.startNew(template);
      });
    });

    // Coming back to a workflow whose test run is still shown opens its tab, once per workflow.
    let shownFor: string | null = null;
    effect(() => {
      const ref = this.store.ref();
      if (this.store.load() !== 'ready' || !ref) return;
      const key = `${ref.skillId}/${ref.name}`;
      if (shownFor === key) return;
      shownFor = key;
      if (untracked(() => this.testRuns.recordFor(ref))) this.tab.set('test');
    });

    // The test run was cancelled on its sheet, or closed: back to the Preview it started from.
    effect(() => {
      if (this.tab() === 'test' && !this.testRecord()) untracked(() => this.tab.set('preview'));
    });
  }

  /** The workflow's test run, while the designer shows one (PR-D.4). */
  protected readonly testRecord = computed(() => this.testRuns.recordFor(this.store.ref()));

  /** The test run's run, once Start was pressed on its sheet. */
  private readonly testRun = computed<WorkflowRunView | null>(() => {
    const run = this.testRuns.runFor(this.store.ref());
    return isStartedRun(run) ? run : null;
  });

  /** The canvas draws the run while its tab is open and the run has started (§7 D4). */
  protected readonly graphMode = computed<GraphMode>(() => (this.tab() === 'test' && this.testRun() ? 'run' : 'design'));

  private readonly testProgress = computed(() => {
    const run = this.testRun();
    return run ? this.testRuns.progress(run) : null;
  });

  /** Each draft step's line in the run, matched by the step's saved name (then by its Agent). */
  private readonly testLines = computed(() => {
    const progress = this.testProgress();
    const lines = new Map<string, RunStepLine>();
    if (!progress) return lines;
    for (const step of this.store.draft().steps) {
      const line = lineFor(step, progress.lines);
      if (line) lines.set(step.key, line);
    }
    return lines;
  });

  protected readonly graphStatus = computed<ReadonlyMap<string, GraphStepStatus>>(() => {
    const run = this.testRun();
    const status = new Map<string, GraphStepStatus>();
    if (!run) return status;
    for (const [key, line] of this.testLines()) {
      status.set(key, {
        state: line.state,
        phrase: this.testRuns.phrase(line),
        elapsed: line.state === 'done' ? elapsedLabel(run.approvedAt, line.row?.updatedAt) : undefined,
      });
    }
    return status;
  });

  /** Start has finished once the run started; a step once it is done. */
  protected readonly graphFinished = computed<ReadonlySet<string>>(() => {
    const finished = new Set<string>();
    if (!this.testRun()) return finished;
    finished.add(START_NODE);
    for (const [key, line] of this.testLines()) if (line.state === 'done') finished.add(key);
    return finished;
  });

  protected readonly savedLabel = computed(() => {
    switch (this.store.savedState()) {
      case 'saving':
        return 'Saving…';
      case 'new':
        return 'Not saved yet';
      case 'unsaved':
        return 'Unsaved changes';
      default:
        return `Saved · version ${this.store.version()}`;
    }
  });

  protected readonly startCard = computed(() => ({
    title: 'Starts with',
    meta: this.graphMode() === 'run' ? `Started · ${START_META[this.store.draft().files]}` : START_META[this.store.draft().files],
  }));

  protected readonly resultCard = computed(() => {
    const merge = this.store.draft().result.mode === 'merge';
    const title = merge ? 'A merge' : 'One card';
    const progress = this.graphMode() === 'run' ? this.testProgress() : null;
    if (progress) {
      const finished = `${progress.done} of ${progress.total} finished`;
      return { title, meta: this.testRun()?.resultDeliveryId ? `Results ready · ${finished}` : finished };
    }
    // What the Result reads, not every step: "Reads: summary" is one result (§9.1).
    const count = this.store.resultSteps().length;
    const where = merge ? 'Starts by itself' : 'Back to chat';
    return { title, meta: count ? `${where} · ${count} ${count === 1 ? 'result' : 'results'}` : where };
  });

  protected readonly graphStages = computed<GraphStepCard[][]>(() => {
    const byKey = new Map(this.store.draft().steps.map((step) => [step.key, step]));
    return this.store.stages().map((keys) =>
      keys.map((key) => {
        const step = byKey.get(key)!;
        const model = this.store.modelLabel(step);
        if (step.kind === 'prompt' || step.kind === 'checklist') {
          const judged = this.store.modelViewFor(step);
          const checklist = step.kind === 'checklist';
          let warning: string | undefined;
          if (!step.modelId) warning = 'Choose a model';
          else if (judged && !judged.canRun) warning = 'You can’t use this model here';
          else if (checklist && !step.fieldSets.length) warning = 'Choose a field set';
          else if (checklist && !step.reads.includes('files')) warning = 'Needs the run’s files';
          else if (checklist && step.fieldSets.some((name) => this.store.fieldSetView(name)?.canRun === false)) {
            warning = 'A field set can’t run';
          }
          const kindWord = checklist ? 'Checklist' : 'Prompt';
          // A checklist card also says how many sets it runs: one model call each (§22.3).
          const sets = checklist && step.fieldSets.length ? ` · ${fieldSetsLabel(step.fieldSets.length)}` : '';
          return {
            key,
            kind: step.kind,
            title: this.store.stepLabel(step),
            meta: `${model ? `${kindWord} · ${model}` : kindWord}${sets}`,
            agentId: null,
            warning,
            readsLabel: this.store.readsLabel(step),
          };
        }
        const { mention, view } = this.store.agentFor(step);
        let warning: string | undefined;
        if (!step.agentId) warning = 'Choose an Agent';
        else if (view && !view.canRun) warning = 'You can’t use this Agent';
        return {
          key,
          title: this.store.stepLabel(step),
          meta: model ? `Agent · ${model}` : 'Agent',
          agentId: step.agentId,
          emoji: mention?.emoji ?? view?.emoji,
          iconUrl: mention?.iconUrl,
          warning,
          readsLabel: this.store.readsLabel(step),
        };
      }),
    );
  });

  /** Starts with → stage 1, each step → the steps that read it, each result step → Result. */
  protected readonly graphEdges = computed<GraphEdge[]>(() => {
    const steps = this.store.draft().steps;
    const stages = this.store.stages();
    const byName = new Map(steps.map((step) => [step.name, step.key]));
    const edges: GraphEdge[] = (stages[0] ?? []).map((key) => [START_NODE, key] as const);
    for (const step of steps) {
      for (const read of stepReads(step)) {
        const from = byName.get(read);
        if (from) edges.push([from, step.key]);
      }
    }
    const takes = new Set(this.store.resultSteps());
    for (const step of steps) {
      if (takes.has(step.name)) edges.push([step.key, RESULT_NODE]);
    }
    return edges;
  });

  /** The panel's tabs: Step and Preview, and Test run while there is one. */
  protected readonly tabs = computed<PanelTab[]>(() => (this.testRecord() ? ['step', 'preview', 'test'] : ['step', 'preview']));

  protected setTab(tab: PanelTab): void {
    this.tab.set(tab);
  }

  /** Arrow keys move between the tabs, as a tablist does, wrapping at either end. */
  protected onTabKeydown(event: KeyboardEvent): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const tabs = this.tabs();
    const index = tabs.indexOf(this.tab());
    const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    this.tab.set(next);
    document.getElementById(`wf-tab-${next}`)?.focus();
  }

  /** The test run's sheet is up: show it. */
  protected showTestRun(): void {
    this.tab.set('test');
  }

  protected selectNode(node: string): void {
    this.store.select(node);
    this.tab.set('step');
  }

  protected addStep(stage = 0): void {
    const key = this.store.add(stage);
    if (!key) return;
    this.tab.set('step');
    // With prompt steps on, the Step tab opens on the choice of kind, so focus goes there first.
    const first = this.store.promptStepsOn ? 'wf-kind-agent' : 'wf-agent-filter';
    afterNextRender(() => document.getElementById(first)?.focus(), { injector: this.injector });
  }

  protected focusCanvas(node: string): void {
    afterNextRender(() => this.graph()?.focusNode(node), { injector: this.injector });
  }

  protected titleOf(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  protected async save(): Promise<void> {
    const wasNew = this.store.ref() === null;
    const ref = await this.store.save();
    if (ref && wasNew) {
      this.location.replaceState(`/workflows/${encodeURIComponent(ref.skillId)}/${encodeURIComponent(ref.name)}`);
    }
  }

  protected async confirmDelete(): Promise<void> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete this workflow?',
        message: `“${this.store.draft().title}” is removed for everyone who can use it. Runs already started keep their conversations.`,
        confirmText: 'Delete workflow',
        destructive: true,
      } satisfies ConfirmationDialogData,
    });
    if ((await firstValueFrom(ref.closed)) !== true) return;
    if (await this.store.remove()) void this.router.navigate(['/workflows']);
  }
}

/** A draft step's line in a run: the run's step with the same saved name, else the same Agent. */
function lineFor(step: DraftStep, lines: readonly RunStepLine[]): RunStepLine | undefined {
  return (
    lines.find((line) => line.step.stepName === step.name) ??
    lines.find((line) => !line.step.stepName && !!step.agentId && line.step.agentId === step.agentId)
  );
}

/**
 * "1m 04s": from the run's approval to the step's handoff row settling. A queued step's wait for a
 * slot is in it, which is what the person waited.
 */
export function elapsedLabel(from: string | undefined, to: string | undefined): string | undefined {
  if (!from || !to) return undefined;
  const ms = Date.parse(to) - Date.parse(from);
  if (!Number.isFinite(ms) || ms < 0) return undefined;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}
