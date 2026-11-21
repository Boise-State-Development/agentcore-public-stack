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

export interface WorkflowStepView {
  name: string;
  kind: string;
  agentId: string;
  agentVersion?: number;
  /** `request`, `files`, then other steps' names. */
  reads: string[];
  request?: string;
  effectiveRequest: string;
  agent: WorkflowAgentView;
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

export type WorkflowSheetBlockerCode = 'too_few_steps' | 'step_unavailable' | 'same_agent_twice' | 'reads_step' | 'merge_result';

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
    agentId: string;
    agentVersion?: number | null;
    reads: string[];
    request?: string | null;
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
