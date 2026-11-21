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
import { GraphEdge, GraphStepCard, WorkflowGraphComponent } from './components/workflow-graph.component';
import { WorkflowPreviewTabComponent } from './components/workflow-preview-tab.component';
import { WorkflowStepTabComponent } from './components/workflow-step-tab.component';
import { stepReads } from './workflow-draft';
import { RESULT_NODE, START_NODE, WorkflowDesignerStore } from './workflow-designer.store';
import { MAX_WORKFLOW_STEPS, WorkflowFiles } from './workflow.models';

type PanelTab = 'step' | 'preview';

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
  imports: [NgIcon, RouterLink, WorkflowGraphComponent, WorkflowStepTabComponent, WorkflowPreviewTabComponent],
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
  }

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
    meta: START_META[this.store.draft().files],
  }));

  protected readonly resultCard = computed(() => {
    const count = this.store.stepCount();
    return {
      title: 'One card',
      meta: count ? `Back to chat · ${count} ${count === 1 ? 'result' : 'results'}` : 'Back to chat',
    };
  });

  protected readonly graphStages = computed<GraphStepCard[][]>(() => {
    const byKey = new Map(this.store.draft().steps.map((step) => [step.key, step]));
    return this.store.stages().map((keys) =>
      keys.map((key) => {
        const step = byKey.get(key)!;
        const { mention, view } = this.store.agentFor(step);
        const model = this.store.modelLabel(step);
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
    const takes = this.store.draft().result.reads;
    for (const step of steps) {
      if (takes === null || takes.includes(step.name)) edges.push([step.key, RESULT_NODE]);
    }
    return edges;
  });

  protected setTab(tab: PanelTab): void {
    this.tab.set(tab);
  }

  /** Arrow keys move between the two tabs, as a tablist does. */
  protected onTabKeydown(event: KeyboardEvent): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const next: PanelTab = this.tab() === 'step' ? 'preview' : 'step';
    this.tab.set(next);
    document.getElementById(`wf-tab-${next}`)?.focus();
  }

  protected selectNode(node: string): void {
    this.store.select(node);
    this.tab.set('step');
  }

  protected addStep(): void {
    const key = this.store.add();
    if (!key) return;
    this.tab.set('step');
    afterNextRender(() => document.getElementById('wf-agent-filter')?.focus(), { injector: this.injector });
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
