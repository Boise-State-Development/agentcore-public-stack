/**
 * Saved workflows on the wire (docs/specs/workflow-designer.md §9.2, §20.3 "Built (PR-D.1)").
 * Shapes mirror app-api's `/workflows` routes and `apis.shared.workflows.sheet`. The SPA never
 * parses or writes the file: it reads and sends these.
 */

/** Names one workflow: the skill that carries it, and the file's name in it. */
export interface WorkflowRef {
  skillId: string;
  name: string;
}

/** One row of `GET /workflows`. `problem` is set, and the rest absent, when the file no longer parses. */
export interface WorkflowSummary extends WorkflowRef {
  skillName: string;
  title?: string;
  version?: number;
  stepCount?: number;
  isOwned: boolean;
  designerMade: boolean;
  problem?: string;
}

export interface WorkflowListResponse {
  workflows: WorkflowSummary[];
}

/** `none`, exactly `one`, or `any` files a run takes. */
export type WorkflowFilesRule = 'none' | 'one' | 'any';

/** A step's Agent as the caller sees it. There is no name when the caller can't use it. */
export interface WorkflowAgentView {
  canRun: boolean;
  name?: string;
  emoji?: string;
  imageUrl?: string;
  hasIcon: boolean;
  modelId?: string;
  inputPricePerMillionTokens?: number;
  publishedVersion?: number;
  /** The Agent's published version moved since the step recorded it (§10). */
  changed: boolean;
  /** The caller owns the Agent, so their run uses its draft (§10). */
  runsDraft: boolean;
}

/** Why the caller can't run a prompt step's model (`ModelView.reason`). */
export type WorkflowModelUnavailableReason = 'no_catalog_row' | 'no_access' | 'retired' | 'unsupported' | 'unavailable';

/**
 * A prompt step's model as the caller sees it (workflow-designer §21.3). `modelId` is the id the
 * step runs on, after retirement; `requestedModelId` is the file's, when that differs.
 */
export interface WorkflowModelView {
  canRun: boolean;
  modelId: string;
  requestedModelId?: string;
  name?: string;
  inputPricePerMillionTokens?: number;
  reason?: WorkflowModelUnavailableReason;
}

/**
 * `agent` (an Agent step), `prompt` (one model call, no Agent, §21.3) or `checklist` (one model
 * call per field set, no Agent, §22.3).
 */
export type WorkflowStepKind = 'agent' | 'prompt' | 'checklist';

/** Why a checklist step's field set can't run (`FieldSetView.reason`). */
export type WorkflowFieldSetUnavailableReason = 'missing' | 'unreadable';

/**
 * One field set a checklist step runs, as the workflow's skill holds it now (`FieldSetView`,
 * §22.3). `fieldCount` is how many fields its call asks for, which the estimate reads.
 */
export interface WorkflowFieldSetView {
  name: string;
  canRun: boolean;
  title?: string;
  version?: string;
  fieldCount?: number;
  reason?: WorkflowFieldSetUnavailableReason;
}

export interface WorkflowStepView {
  name: string;
  kind: string;
  /** Empty on a prompt or checklist step. */
  agentId: string;
  agentVersion?: number;
  /** A prompt or checklist step's model, as the file names it. */
  modelId?: string;
  /** `request`, `files`, then other steps' names. */
  reads: string[];
  /** Its own request; a checklist step's guidance. */
  request?: string;
  effectiveRequest: string;
  /** The step's worker. On a prompt or checklist step, its model in this shape (name, modelId, rate, canRun). */
  agent: WorkflowAgentView;
  /** A prompt or checklist step's model as the caller sees it. */
  model?: WorkflowModelView;
  /** A checklist step's field sets, in the order it runs them, each as the skill holds it now. */
  fieldSets?: WorkflowFieldSetView[];
  /** Model calls the step makes: one per field set on a checklist step, one on a prompt step. */
  calls?: number;
}

export interface WorkflowResultView {
  mode: string;
  reads?: string[];
  steps: string[];
  instruction?: string;
}

export interface WorkflowView extends WorkflowRef {
  title: string;
  version: number;
  files: WorkflowFilesRule;
  request?: string;
  steps: WorkflowStepView[];
  result: WorkflowResultView;
  fingerprint: string;
  isOwned: boolean;
}

export type WorkflowSheetBlockerCode =
  | 'too_few_steps'
  | 'step_unavailable'
  | 'same_agent_twice'
  | 'reads_step'
  /** A prompt step while `PROMPT_STEPS_ENABLED` is off. */
  | 'prompt_steps_off'
  /** A prompt or checklist step on a model the caller can't run (`model.reason` says why). */
  | 'model_unavailable'
  /** A checklist step while `CHECKLIST_STEPS_ENABLED` is off. */
  | 'checklist_steps_off'
  /** A checklist step naming a set the skill no longer has, or one with a `list` field. */
  | 'field_set_unavailable';

/** The blockers a step carries that its removal clears: the run starts without it (§20.1). */
export const STEP_BLOCKER_CODES: ReadonlySet<WorkflowSheetBlockerCode> = new Set([
  'step_unavailable',
  'prompt_steps_off',
  'model_unavailable',
  'checklist_steps_off',
  'field_set_unavailable',
]);

export interface WorkflowSheetBlocker {
  code: WorkflowSheetBlockerCode;
  message: string;
  step?: string;
}

export interface WorkflowSheet {
  startable: boolean;
  blockers: WorkflowSheetBlocker[];
  /** A merged Result only (PR-D.5b): what the one merge turn will ask, the file's instruction or the default. */
  mergeInstruction?: string;
}

/** `GET`, `POST` and `PUT` on `/workflows` all answer with this. */
export interface WorkflowDocument {
  workflow: WorkflowView;
  sheet: WorkflowSheet;
  /** PUT only: false for `?validate=true` and for a save that changed nothing. */
  written?: boolean;
}

/** The body of `PUT /workflows/{skillId}/{name}`. `version` is the server's and is never sent. */
export interface WorkflowDefinition {
  title: string;
  files: WorkflowFilesRule;
  request?: string | null;
  steps: {
    name: string;
    kind: string;
    /** Empty on a prompt or checklist step. */
    agentId: string;
    agentVersion?: number | null;
    /** A prompt or checklist step's model; absent on an Agent step. */
    modelId?: string | null;
    reads: string[];
    /** Its own request; a checklist step's guidance. */
    request?: string | null;
    /** A checklist step's field sets, in the order it runs them; absent on every other kind. */
    fieldSets?: string[];
  }[];
  result: { mode: string; reads?: string[] | null; instruction?: string | null };
}

/** The saved workflow a run was started from, on its `WFRUN#` row (PR-D.2). */
export interface WorkflowRunSource extends WorkflowRef {
  title: string;
  fingerprint: string;
  version?: number;
  files?: WorkflowFilesRule;
  /** Steps the starter took out before the run opened, with every step that read them. */
  removedSteps?: string[];
}
