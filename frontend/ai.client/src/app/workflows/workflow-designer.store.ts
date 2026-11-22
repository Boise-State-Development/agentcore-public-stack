import { LiveAnnouncer } from '@angular/cdk/a11y';
import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { AgentMentionService, MentionableAgent } from '../agents/services/agent-mention.service';
import { MySkillService } from '../customize/skills/services/my-skill.service';
import { fieldSetNameFor } from '../customize/skills/models/field-set.model';
import { FEATURES } from '../services/features';
import { fieldSetsLabel } from '../session/services/handoff/workflow-run-progress';
import { isNestedSkillFile } from '../shared/utils/skill-resource-path';
import { ModelService } from '../session/services/model/model.service';
import { promptModelChoices } from './prompt-models';
import {
  DraftStep,
  DraftStepKind,
  WorkflowDraft,
  addStep,
  agentViews,
  blankDraft,
  chooseAgent,
  choosePromptModel,
  fieldSetViews as fieldSetViewsOf,
  isModelDraftStep,
  moveFieldSet,
  toggleFieldSet,
  hasWorker,
  modelViews as modelViewsOf,
  renameStep,
  setStepKind,
  draftFrom,
  isComplete,
  moveStep,
  rekeyed,
  readersOf,
  removeStep,
  resultSteps,
  sameDraft,
  setResult,
  stagesOf,
  stepReads,
  templateDraft,
  toWrite,
  toggleResultRead,
  updateStep,
} from './workflow-draft';
import {
  MAX_WORKFLOW_STEPS,
  WorkflowAgentView,
  WorkflowDocument,
  WorkflowFieldSetView,
  WorkflowFiles,
  WorkflowModelView,
  WorkflowRef,
  WorkflowResultMode,
} from './workflow.models';
import { WorkflowsApiService } from '../services/workflows/workflows-api.service';

export type DesignerLoad = 'loading' | 'ready' | 'not_found' | 'broken' | 'error';

/** The workflow's skill's field sets as the Step tab lists them: by name, from its supporting files. */
export type SkillFieldSets =
  | { state: 'idle' | 'loading' | 'error' }
  | { state: 'ready'; names: string[] };

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
  private readonly workflows = inject(WorkflowsApiService);
  private readonly mentions = inject(AgentMentionService);
  private readonly models = inject(ModelService);
  private readonly announcer = inject(LiveAnnouncer);
  private readonly features = inject(FEATURES);
  private readonly skills = inject(MySkillService);

  /** The designer offers prompt steps (`features.promptSteps`, workflow-designer §21.3). */
  readonly promptStepsOn = this.features.promptSteps;
  /** And checklist steps (`features.checklistSteps`, §22.3), which run on prompt steps' invocation. */
  readonly checklistStepsOn = this.features.promptSteps && this.features.checklistSteps;
  /** A checklist step's sets link to the field-set editor (`features.fieldSetAuthoring`). */
  readonly fieldSetAuthoringOn = this.features.fieldSetAuthoring;

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
    const missing = this.draft().steps.filter((step) => !hasWorker(step));
    const agents = missing.filter((step) => !isModelDraftStep(step)).length;
    const models = missing.filter((step) => step.kind === 'prompt').length;
    const checklists = missing.filter((step) => step.kind === 'checklist');
    const parts: string[] = [];
    if (agents === 1) parts.push('Choose an Agent for the step that has none.');
    if (agents > 1) parts.push(`Choose an Agent for the ${agents} steps that have none.`);
    if (models === 1) parts.push('Choose a model for the prompt step that has none.');
    if (models > 1) parts.push(`Choose a model for the ${models} prompt steps that have none.`);
    for (const step of checklists) parts.push(`${this.stepLabel(step)} needs ${checklistNeeds(step)}.`);
    return parts.length ? parts.join(' ') : null;
  });

  /** The sheet describes the draft as it is now. */
  readonly previewCurrent = computed(() => {
    const described = this.documentDraft();
    return described !== null && sameDraft(described, this.draft());
  });

  readonly agents = computed(() => this.mentions.mentionable());
  private readonly agentById = computed(() => new Map(this.agents().map((agent) => [agent.agentId, agent])));
  readonly views = computed(() => agentViews(this.document()));
  /** Each prompt step's model as the server last judged it for this user, by step name. */
  readonly modelViews = computed(() => modelViewsOf(this.document()));
  /** The models a prompt step can be given: ones this user can use that run a prompt step. */
  readonly promptModels = computed(() => promptModelChoices(this.models.availableModels()));
  /** Each field set the server last read for a checklist step, by name (title, field count, can it run). */
  readonly fieldSetViews = computed(() => fieldSetViewsOf(this.document()));

  /**
   * The field sets in this workflow's skill, which a checklist step picks from (§22.3): read from
   * the skill's supporting files by filename (`field-set.<name>.md`), never by parsing one. The
   * server judges the chosen ones on every preview (`fieldSets` on the step), and refuses a set the
   * skill doesn't hold. Read only for a saved workflow the user owns, and only with checklist steps on.
   */
  readonly skillFieldSets = signal<SkillFieldSets>({ state: 'idle' });
  private skillFieldSetsFor: string | null = null;

  readonly stages = computed(() => stagesOf(this.draft().steps));
  readonly stepCount = computed(() => this.draft().steps.length);
  readonly atCap = computed(() => this.stepCount() >= MAX_WORKFLOW_STEPS);
  /** The steps the Result takes, by name; every step while the Result names none. */
  readonly resultSteps = computed(() => resultSteps(this.draft()));

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
    // The skill's field sets, once per workflow, for the checklist step's picker.
    effect(() => {
      const skillId = this.ref()?.skillId ?? null;
      const owned = this.isOwned();
      if (!this.checklistStepsOn || !skillId || !owned || skillId === this.skillFieldSetsFor) return;
      untracked(() => void this.loadSkillFieldSets(skillId));
    });

    effect(() => {
      const draft = this.draft();
      const ref = this.ref();
      // A save in flight describes the draft itself; the preview waits for it.
      if (!ref || this.load() !== 'ready' || this.saving() || !isComplete(draft) || !this.isOwned()) return;
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

  /**
   * Read the workflow's skill's field-set names again (Refresh, after a set is added in another
   * tab). A failed read leaves the picker saying so; the chosen sets are still the server's to judge.
   */
  async loadSkillFieldSets(skillId = this.ref()?.skillId ?? null): Promise<void> {
    if (!skillId) return;
    this.skillFieldSetsFor = skillId;
    this.skillFieldSets.set({ state: 'loading' });
    try {
      const skill = await this.skills.getSkill(skillId);
      if (this.ref()?.skillId !== skillId) return;
      const names = skill.resources
        .filter((resource) => resource.kind === 'reference' && !isNestedSkillFile(resource))
        .map((resource) => fieldSetNameFor(resource.filename))
        .filter((name): name is string => !!name)
        .sort();
      this.skillFieldSets.set({ state: 'ready', names });
    } catch {
      if (this.ref()?.skillId === skillId) this.skillFieldSets.set({ state: 'error' });
    }
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
      const result = await this.workflows.write(ref, body);
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

  /**
   * Add an Agent step to `stage` (0-based; one past the last starts a new stage) and select it.
   * Returns its key, or null at the cap.
   */
  add(stage = 0): string | null {
    const added = addStep(this.draft(), stage);
    if (!added) return null;
    this.draft.set(added.draft);
    this.selected.set(added.key);
    this.announcer.announce(
      `Step added to stage ${this.stageOf(added.key)}. ${this.stepCount()} of ${MAX_WORKFLOW_STEPS} steps`,
      'polite',
    );
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

  /** Make a step an Agent step, a prompt step or a checklist step, and say so. */
  setStepKind(key: string, kind: DraftStepKind): void {
    const before = this.draft();
    const after = setStepKind(before, key, kind);
    if (after === before) return;
    this.draft.set(after);
    const said: Record<DraftStepKind, string> = {
      agent: 'Now an Agent step. Choose its Agent.',
      prompt: 'Now a prompt step. Choose its model.',
      checklist: 'Now a checklist step. Choose its model and its field sets.',
    };
    this.announcer.announce(said[kind], 'polite');
  }

  /** Add a field set to a checklist step, or take it off, and say so. */
  toggleFieldSet(key: string, name: string): void {
    const before = this.draft();
    const after = toggleFieldSet(before, key, name);
    if (after === before) return;
    this.draft.set(after);
    const sets = after.steps.find((s) => s.key === key)?.fieldSets ?? [];
    const title = this.fieldSetTitle(name);
    this.announcer.announce(
      `${title} ${sets.includes(name) ? 'added' : 'removed'}. ${fieldSetsLabel(sets.length)} on this step`,
      'polite',
    );
  }

  /** Move one of a checklist step's field sets, and say where it is now. */
  moveFieldSet(key: string, name: string, delta: -1 | 1): void {
    const before = this.draft();
    const after = moveFieldSet(before, key, name, delta);
    if (after === before) return;
    this.draft.set(after);
    const sets = after.steps.find((s) => s.key === key)?.fieldSets ?? [];
    this.announcer.announce(`${this.fieldSetTitle(name)} moved to position ${sets.indexOf(name) + 1} of ${sets.length}`, 'polite');
  }

  /** A field set as the author reads it: its title once the server has read it, else its name. */
  fieldSetTitle(name: string): string {
    return this.fieldSetViews().get(name)?.title?.trim() || name;
  }

  /** A field set as the server last read it for a checklist step, or null before a preview has. */
  fieldSetView(name: string): WorkflowFieldSetView | null {
    return this.fieldSetViews().get(name) ?? null;
  }

  choosePromptModel(key: string, modelId: string): void {
    this.draft.set(choosePromptModel(this.draft(), key, modelId));
  }

  /** Rename a step; what other steps and the Result read follows the new name. */
  renameStep(key: string, label: string): void {
    this.draft.set(renameStep(this.draft(), key, label));
  }

  /** Set what a step reads, and say so when that moves it to another stage (§4.4). */
  setReads(key: string, reads: string[]): void {
    const before = this.stageOf(key);
    this.draft.set(updateStep(this.draft(), key, { reads }));
    const after = this.stageOf(key);
    if (after === before) return;
    const step = this.draft().steps.find((s) => s.key === key);
    this.announcer.announce(`${step ? this.stepLabel(step) : 'Step'} moved to stage ${after}`, 'polite');
  }

  /** The steps `key` can't read, because each already reads it, with the step it reads on the way. */
  readersOf(key: string): Map<string, string | null> {
    return readersOf(this.draft().steps, key);
  }

  setResultMode(mode: WorkflowResultMode): void {
    this.draft.set(setResult(this.draft(), { mode }));
  }

  setResultInstruction(instruction: string): void {
    this.draft.set(setResult(this.draft(), { instruction }));
  }

  /** Include or leave out one step's result; the last one can't be left out. */
  toggleResultRead(name: string): void {
    this.draft.set(toggleResultRead(this.draft(), name));
  }

  /** A step's stage, 1-based. */
  stageOf(key: string): number {
    return this.stages().findIndex((keys) => keys.includes(key)) + 1;
  }

  setOwnRequest(key: string, ownRequest: string | null): void {
    this.draft.set(updateStep(this.draft(), key, { ownRequest }));
  }

  // ---- what a card says -----------------------------------------------------------------------

  /** A step's Agent as known: the mention list for name and icon, the server for the rest. */
  agentFor(step: DraftStep): { mention: MentionableAgent | null; view: WorkflowAgentView | null } {
    if (isModelDraftStep(step) || !step.agentId) return { mention: null, view: null };
    return { mention: this.agentById().get(step.agentId) ?? null, view: this.views().get(step.agentId) ?? null };
  }

  /**
   * What a step is called: its Agent's name, else (no Agent yet) its role from the step name, as
   * the template's "Fiscal outsider", or "New step" for one just added.
   */
  stepLabel(step: DraftStep): string {
    if (isModelDraftStep(step)) {
      // Several model steps can share a model, so a prompt or checklist step is called by its own name.
      const kind = step.kind === 'checklist' ? 'checklist' : 'prompt';
      if (step.autoName) return `New ${kind} step`;
      const words = step.name.replace(/_/g, ' ').trim();
      return words ? words.charAt(0).toUpperCase() + words.slice(1) : kind === 'checklist' ? 'Checklist step' : 'Prompt step';
    }
    const { mention, view } = this.agentFor(step);
    if (step.agentId) return mention?.name || view?.name || 'An Agent you can’t use';
    if (step.autoName) return 'New step';
    const words = step.name.replace(/_/g, ' ').trim();
    return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'New step';
  }

  /** The Agent's model by name, or what it runs on when it names none. */
  modelLabel(step: DraftStep): string | null {
    if (isModelDraftStep(step)) {
      if (!step.modelId) return null;
      return this.models.modelNameFor(step.modelId) ?? this.modelViewFor(step)?.name ?? step.modelId;
    }
    const { view } = this.agentFor(step);
    if (!view || !view.canRun) return null;
    if (!view.modelId) return 'The starter’s model';
    return this.models.modelNameFor(view.modelId) ?? view.modelId;
  }

  /**
   * A prompt step's model as the server last judged it for this user, while it still names the
   * model the server judged (a model chosen since waits for the next preview).
   */
  modelViewFor(step: DraftStep): WorkflowModelView | null {
    if (!isModelDraftStep(step) || !step.modelId) return null;
    const view = this.modelViews().get(step.name);
    if (!view) return null;
    return view.modelId === step.modelId || view.requestedModelId === step.modelId ? view : null;
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

/** What a checklist step still lacks before it can be saved, in the Save hint's words. */
function checklistNeeds(step: DraftStep): string {
  const needs: string[] = [];
  if (!step.modelId) needs.push('a model');
  if (!step.fieldSets.length) needs.push('a field set');
  if (!step.reads.includes('files')) needs.push('to read the run’s files');
  return needs.length <= 1 ? (needs[0] ?? 'a model') : `${needs.slice(0, -1).join(', ')} and ${needs[needs.length - 1]}`;
}
