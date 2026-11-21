import { LiveAnnouncer } from '@angular/cdk/a11y';
import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { AgentMentionService, MentionableAgent } from '../agents/services/agent-mention.service';
import { ModelService } from '../session/services/model/model.service';
import {
  DraftStep,
  WorkflowDraft,
  addStep,
  agentViews,
  blankDraft,
  chooseAgent,
  draftFrom,
  isComplete,
  moveStep,
  rekeyed,
  removeStep,
  sameDraft,
  stagesOf,
  stepReads,
  templateDraft,
  toWrite,
  updateStep,
} from './workflow-draft';
import {
  MAX_WORKFLOW_STEPS,
  WorkflowAgentView,
  WorkflowDocument,
  WorkflowFiles,
  WorkflowRef,
} from './workflow.models';
import { WorkflowService } from './workflow.service';

export type DesignerLoad = 'loading' | 'ready' | 'not_found' | 'broken' | 'error';

/** How the header says where the draft stands. */
export type SavedState = 'new' | 'saved' | 'unsaved' | 'saving';

/** The fixed cards' ids beside the steps' keys. */
export const START_NODE = 'start';
export const RESULT_NODE = 'result';

/** How long the preview waits after the last edit before it asks the server for the sheet. */
const PREVIEW_DEBOUNCE_MS = 500;

/**
 * The designer's state, scoped to one designer page (docs/specs/workflow-designer.md §5).
 *
 * It holds the draft and what the server last said about it. Every edit is a pure function from
 * `workflow-draft.ts`; this class applies it, keeps the selection, and says what changed through
 * the polite live region (§4.4). The sheet is the server's: a `PUT …?validate=true` with the
 * draft, debounced, which writes nothing and makes no model call (D3).
 */
@Injectable()
export class WorkflowDesignerStore {
  private readonly workflows = inject(WorkflowService);
  private readonly mentions = inject(AgentMentionService);
  private readonly models = inject(ModelService);
  private readonly announcer = inject(LiveAnnouncer);

  readonly ref = signal<WorkflowRef | null>(null);
  readonly load = signal<DesignerLoad>('loading');
  readonly loadMessage = signal<string | null>(null);
  readonly isOwned = signal(true);

  readonly draft = signal<WorkflowDraft>(blankDraft());
  /** The draft as last saved; null for a workflow never saved. */
  private readonly baseline = signal<WorkflowDraft | null>(null);
  readonly version = signal<number | null>(null);
  readonly selected = signal<string>(START_NODE);

  readonly saving = signal(false);
  readonly saveError = signal<string | null>(null);
  /** A save refused because another tab moved the workflow on: what is stored now. */
  readonly conflict = signal<{ message: string; document: WorkflowDocument | null } | null>(null);

  /** The server's last word on this draft (GET, save or validate). */
  readonly document = signal<WorkflowDocument | null>(null);
  readonly validating = signal(false);
  readonly previewError = signal<string | null>(null);
  /** The draft the current `document` describes, so a stale sheet is never shown as current. */
  private readonly documentDraft = signal<WorkflowDraft | null>(null);

  readonly dirty = computed(() => {
    const baseline = this.baseline();
    return baseline === null || !sameDraft(baseline, this.draft());
  });

  readonly savedState = computed<SavedState>(() => {
    if (this.saving()) return 'saving';
    if (this.baseline() === null) return 'new';
    return this.dirty() ? 'unsaved' : 'saved';
  });

  readonly complete = computed(() => isComplete(this.draft()));
  readonly canSave = computed(() => this.isOwned() && this.complete() && this.dirty() && !this.saving());

  /** Why Save is off, in words, or null when it isn't. */
  readonly saveBlockedReason = computed(() => {
    if (!this.isOwned()) return 'Only the workflow’s owner can save it.';
    const missing = this.draft().steps.filter((step) => !step.agentId).length;
    if (missing === 1) return 'Choose an Agent for the step that has none.';
    if (missing > 1) return `Choose an Agent for the ${missing} steps that have none.`;
    return null;
  });

  /** The sheet describes the draft as it is now. */
  readonly previewCurrent = computed(() => {
    const described = this.documentDraft();
    return described !== null && sameDraft(described, this.draft());
  });

  readonly agents = computed(() => this.mentions.mentionable());
  private readonly agentById = computed(() => new Map(this.agents().map((agent) => [agent.agentId, agent])));
  readonly views = computed(() => agentViews(this.document()));

  readonly stages = computed(() => stagesOf(this.draft().steps));
  readonly stepCount = computed(() => this.draft().steps.length);
  readonly atCap = computed(() => this.stepCount() >= MAX_WORKFLOW_STEPS);

  readonly selectedStep = computed<DraftStep | null>(
    () => this.draft().steps.find((step) => step.key === this.selected()) ?? null,
  );

  private previewTimer: ReturnType<typeof setTimeout> | null = null;
  private previewSeq = 0;

  constructor() {
    void this.mentions.load();
    inject(DestroyRef).onDestroy(() => this.clearPreviewTimer());

    // The sheet follows the draft. It needs a saved workflow to validate against, and an Agent
    // on every step, because the body names one per step.
    effect(() => {
      const draft = this.draft();
      const ref = this.ref();
      if (!ref || this.load() !== 'ready' || !isComplete(draft) || !this.isOwned()) return;
      untracked(() => this.schedulePreview(ref, draft));
    });
  }

  // ---- loading --------------------------------------------------------------------------------

  async open(ref: WorkflowRef): Promise<void> {
    this.ref.set(ref);
    this.load.set('loading');
    try {
      const result = await this.workflows.read(ref);
      if (!result.ok) {
        this.loadMessage.set(result.message);
        this.load.set('broken');
        return;
      }
      this.adopt(result.document);
      this.selected.set(START_NODE);
      this.load.set('ready');
    } catch (err) {
      const status = (err as { status?: number } | null)?.status;
      this.load.set(status === 404 ? 'not_found' : 'error');
    }
  }

  /** A workflow not yet saved: blank, or from the one template. Nothing is written until Save. */
  startNew(template: string | null): void {
    this.ref.set(null);
    this.baseline.set(null);
    this.version.set(null);
    this.document.set(null);
    this.documentDraft.set(null);
    this.isOwned.set(true);
    this.draft.set((template && templateDraft(template)) || blankDraft());
    this.selected.set(START_NODE);
    this.load.set('ready');
  }

  private adopt(document: WorkflowDocument, previous?: WorkflowDraft): void {
    const fresh = draftFrom(document.workflow);
    const draft = previous ? rekeyed(fresh, previous) : fresh;
    this.document.set(document);
    this.documentDraft.set(draft);
    this.baseline.set(draft);
    this.draft.set(draft);
    this.version.set(document.workflow.version);
    this.isOwned.set(document.workflow.isOwned);
    this.ref.set({ skillId: document.workflow.skillId, name: document.workflow.name });
  }

  // ---- saving ---------------------------------------------------------------------------------

  /**
   * Save the draft. A new workflow is created first (`POST`), then written against the version it
   * was created at, so the one save is two calls but never clobbers anything. Returns the ref.
   */
  async save(): Promise<WorkflowRef | null> {
    if (!this.canSave()) return null;
    this.saving.set(true);
    this.saveError.set(null);
    this.conflict.set(null);
    const draft = this.draft();
    try {
      let ref = this.ref();
      let expected = this.version() ?? undefined;
      if (!ref) {
        const created = await this.workflows.create({ title: draft.title.trim() || undefined });
        ref = { skillId: created.workflow.skillId, name: created.workflow.name };
        expected = created.workflow.version;
        this.ref.set(ref);
        this.version.set(expected);
        this.baseline.set(draftFrom(created.workflow));
      }
      // A step records its Agent's published version when it is added (D6). An Agent the server
      // hasn't described yet (picked since the last preview) is described first, so the save
      // never leaves a step that reads as "changed" the moment it is saved.
      let views = this.views();
      if (draft.steps.some((step) => step.agentVersion === null && step.agentId && !views.has(step.agentId))) {
        const probe = toWrite(draft, views);
        if (probe) views = agentViews(await this.workflows.validate(ref, probe));
      }
      const body = toWrite(draft, views, expected);
      if (!body) return null;
      const result = await this.workflows.save(ref, body);
      if (result.ok) {
        this.adopt(result.document, draft);
        this.announcer.announce(result.document.written ? 'Workflow saved' : 'No changes to save', 'polite');
        return ref;
      }
      if (result.conflict) {
        this.conflict.set({ message: result.message, document: result.document });
      } else {
        this.saveError.set(result.message);
      }
      return null;
    } catch (err) {
      this.saveError.set(this.workflows.messageFor(err, 'The workflow couldn’t be saved.'));
      return null;
    } finally {
      this.saving.set(false);
    }
  }

  /** Drop this tab's edits for what is stored now (after a conflict). */
  async reloadStored(): Promise<void> {
    const conflict = this.conflict();
    this.conflict.set(null);
    if (conflict?.document) {
      this.adopt(conflict.document);
      this.selected.set(START_NODE);
      this.announcer.announce('Reloaded the saved workflow', 'polite');
      return;
    }
    const ref = this.ref();
    if (ref) await this.open(ref);
  }

  dismissConflict(): void {
    this.conflict.set(null);
  }

  async remove(): Promise<boolean> {
    const ref = this.ref();
    if (!ref) return true;
    try {
      await this.workflows.remove(ref);
      return true;
    } catch (err) {
      this.saveError.set(this.workflows.messageFor(err, 'The workflow couldn’t be deleted.'));
      return false;
    }
  }

  // ---- the preview ----------------------------------------------------------------------------

  private schedulePreview(ref: WorkflowRef, draft: WorkflowDraft): void {
    if (this.previewCurrent()) return;
    this.clearPreviewTimer();
    this.previewTimer = setTimeout(() => void this.runPreview(ref, draft), PREVIEW_DEBOUNCE_MS);
  }

  private clearPreviewTimer(): void {
    if (this.previewTimer !== null) clearTimeout(this.previewTimer);
    this.previewTimer = null;
  }

  private async runPreview(ref: WorkflowRef, draft: WorkflowDraft): Promise<void> {
    const body = toWrite(draft, this.views());
    if (!body) return;
    const seq = ++this.previewSeq;
    this.validating.set(true);
    try {
      const document = await this.workflows.validate(ref, body);
      if (seq !== this.previewSeq) return;
      this.document.set(document);
      this.documentDraft.set(draft);
      this.previewError.set(null);
    } catch (err) {
      if (seq !== this.previewSeq) return;
      this.previewError.set(this.workflows.messageFor(err, 'The preview couldn’t be built.'));
    } finally {
      if (seq === this.previewSeq) this.validating.set(false);
    }
  }

  // ---- edits ----------------------------------------------------------------------------------

  select(node: string): void {
    this.selected.set(node);
  }

  setTitle(title: string): void {
    this.draft.update((d) => ({ ...d, title }));
  }

  setFiles(files: WorkflowFiles): void {
    this.draft.update((d) => ({ ...d, files }));
  }

  setRequest(request: string): void {
    this.draft.update((d) => ({ ...d, request }));
  }

  /** Add an Agent step to stage 1 and select it. Returns its key, or null at the cap. */
  add(): string | null {
    const added = addStep(this.draft());
    if (!added) return null;
    this.draft.set(added.draft);
    this.selected.set(added.key);
    this.announcer.announce(`Step added to stage 1. ${this.stepCount()} of ${MAX_WORKFLOW_STEPS} steps`, 'polite');
    return added.key;
  }

  /** Remove a step. Returns the node to select (and focus) next. */
  removeStep(key: string): string {
    const steps = this.draft().steps;
    const at = steps.findIndex((step) => step.key === key);
    if (at < 0) return this.selected();
    const label = this.stepLabel(steps[at]);
    this.draft.set(removeStep(this.draft(), key));
    const rest = this.draft().steps;
    const next = rest[Math.min(at, rest.length - 1)]?.key ?? START_NODE;
    this.selected.set(next);
    this.announcer.announce(`${label} removed. ${rest.length} of ${MAX_WORKFLOW_STEPS} steps`, 'polite');
    return next;
  }

  /** Move a step within its stage, and say where it is now. */
  move(key: string, delta: -1 | 1): void {
    const before = this.draft();
    const after = moveStep(before, key, delta);
    if (after === before) return;
    this.draft.set(after);
    const stages = this.stages();
    const s = stages.findIndex((keys) => keys.includes(key));
    const position = stages[s].indexOf(key) + 1;
    const step = after.steps.find((x) => x.key === key);
    const label = step ? this.stepLabel(step) : 'Step';
    this.announcer.announce(`${label} moved to position ${position} of ${stages[s].length} in stage ${s + 1}`, 'polite');
  }

  chooseAgent(key: string, agent: MentionableAgent): void {
    this.draft.set(chooseAgent(this.draft(), key, agent.agentId, agent.name));
  }

  setReads(key: string, reads: string[]): void {
    this.draft.set(updateStep(this.draft(), key, { reads }));
  }

  setOwnRequest(key: string, ownRequest: string | null): void {
    this.draft.set(updateStep(this.draft(), key, { ownRequest }));
  }

  // ---- what a card says -----------------------------------------------------------------------

  /** A step's Agent as known: the mention list for name and icon, the server for the rest. */
  agentFor(step: DraftStep): { mention: MentionableAgent | null; view: WorkflowAgentView | null } {
    if (!step.agentId) return { mention: null, view: null };
    return { mention: this.agentById().get(step.agentId) ?? null, view: this.views().get(step.agentId) ?? null };
  }

  /**
   * What a step is called: its Agent's name, else (no Agent yet) its role from the step name, as
   * the template's "Fiscal outsider", or "New step" for one just added.
   */
  stepLabel(step: DraftStep): string {
    const { mention, view } = this.agentFor(step);
    if (step.agentId) return mention?.name || view?.name || 'An Agent you can’t use';
    if (step.autoName) return 'New step';
    const words = step.name.replace(/_/g, ' ').trim();
    return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'New step';
  }

  /** The Agent's model by name, or what it runs on when it names none. */
  modelLabel(step: DraftStep): string | null {
    const { view } = this.agentFor(step);
    if (!view || !view.canRun) return null;
    if (!view.modelId) return 'The starter’s model';
    return this.models.modelNameFor(view.modelId) ?? view.modelId;
  }

  /** "reads the request and the files", from the step's reads. */
  readsLabel(step: DraftStep): string {
    const parts: string[] = [];
    if (step.reads.includes('request')) parts.push('the request');
    if (step.reads.includes('files')) parts.push('the files');
    const results = stepReads(step).length;
    if (results) parts.push(`${results} ${results === 1 ? 'result' : 'results'}`);
    if (!parts.length) return 'reads nothing';
    return `reads ${parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`}`;
  }
}
