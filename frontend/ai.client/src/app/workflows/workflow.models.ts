/**
 * Saved workflows (docs/specs/workflow-designer.md §9.2, PR-D.1 "Built" in §20.3). Shapes mirror
 * app-api's `apis/app_api/workflows/models.py`; keep the two in step (CLAUDE.md cross-package
 * contract).
 *
 * The SPA never parses or writes the file (`references/workflow.<name>.md`, D1): it edits this
 * JSON and the server owns the format both ways.
 */

/** `WORKFLOW_RUN_MAX_STEPS`, which the parser pins too. The canvas shows it from the start. */
export const MAX_WORKFLOW_STEPS = 8;

/** `WORKFLOW_RUN_CONCURRENCY`: a stage with more steps than this runs the rest in a queue. */
export const WORKFLOW_STAGE_CONCURRENCY = 4;

/** `WORKFLOW_RUN_MIN_STEPS`: step 1's engine starts no run of fewer. */
export const MIN_WORKFLOW_STEPS = 2;

/** The two reads every step can take besides other steps' results. */
export const INPUT_READS = ['request', 'files'] as const;
export type InputRead = (typeof INPUT_READS)[number];

export type WorkflowFiles = 'none' | 'one' | 'any';
export type WorkflowResultMode = 'card' | 'merge';
export type WorkflowStepKind = 'agent' | 'prompt' | 'checklist';

/** A step's Agent as the caller sees it. No name when `canRun` is false. */
export interface WorkflowAgentView {
  canRun: boolean;
  name?: string;
  emoji?: string;
  imageUrl?: string;
  hasIcon?: boolean;
  /** The Agent's own model; absent when it runs on the send's model. */
  modelId?: string;
  inputPricePerMillionTokens?: number;
  publishedVersion?: number;
  /** The published version moved since the step recorded it (§10). */
  changed?: boolean;
  /** The caller owns the Agent, so their test runs its draft (§10). */
  runsDraft?: boolean;
}

export interface WorkflowStepView {
  name: string;
  kind: WorkflowStepKind;
  agentId: string;
  agentVersion?: number;
  reads: string[];
  request?: string;
  effectiveRequest: string;
  agent: WorkflowAgentView;
}

export interface WorkflowResultView {
  mode: WorkflowResultMode;
  reads?: string[];
  /** The steps the result takes, in table order. */
  steps: string[];
  instruction?: string;
}

export interface WorkflowView {
  skillId: string;
  name: string;
  title: string;
  version: number;
  files: WorkflowFiles;
  request?: string;
  steps: WorkflowStepView[];
  result: WorkflowResultView;
  fingerprint: string;
  isOwned: boolean;
}

export type SheetBlockerCode =
  | 'too_few_steps'
  | 'step_unavailable'
  | 'same_agent_twice'
  | 'reads_step'
  | 'merge_result';

export interface SheetBlocker {
  code: SheetBlockerCode;
  message: string;
  step?: string;
}

export interface WorkflowSheet {
  startable: boolean;
  blockers: SheetBlocker[];
}

/** `GET`, `POST` and `PUT` answer with this. `written` is PUT only. */
export interface WorkflowDocument {
  workflow: WorkflowView;
  sheet: WorkflowSheet;
  written?: boolean;
}

export interface WorkflowSummary {
  skillId: string;
  skillName: string;
  name: string;
  title?: string;
  version?: number;
  stepCount?: number;
  isOwned: boolean;
  designerMade: boolean;
  /** Why the stored file no longer parses; title, version and stepCount are then absent. */
  problem?: string;
}

export interface WorkflowListResponse {
  workflows: WorkflowSummary[];
}

/** One step of a `PUT` body. */
export interface WorkflowStepWrite {
  name: string;
  kind: WorkflowStepKind;
  agentId: string;
  agentVersion?: number | null;
  reads: string[];
  request?: string | null;
}

/** The `PUT` body. `expectedVersion` makes a save refuse to clobber another tab's (409). */
export interface WorkflowWrite {
  title: string;
  files: WorkflowFiles;
  request?: string | null;
  steps: WorkflowStepWrite[];
  result: { mode: WorkflowResultMode; reads?: string[] | null; instruction?: string | null };
  expectedVersion?: number;
}

/** Which workflow a start or a link names. */
export interface WorkflowRef {
  skillId: string;
  name: string;
}

/** The designer's route for a workflow. */
export function designerLink(ref: WorkflowRef): string[] {
  return ['/workflows', ref.skillId, ref.name];
}

/**
 * The workflow name a carrying skill's supporting file holds, from its filename alone
 * (`workflow.<name>.md`), or null. A name, not the format: the Customize badge links a skill to
 * its designer with it, as `fieldSetNameFor` does for field sets.
 */
export function workflowNameFor(filename: string): string | null {
  const match = /^workflow\.([a-z0-9]+(?:-[a-z0-9]+)*)\.md$/.exec(filename);
  return match ? match[1] : null;
}
