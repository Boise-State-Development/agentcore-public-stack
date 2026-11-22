/**
 * The designer's constants and names. The wire shapes are PR-D.2's
 * (`services/workflows/workflow.models.ts`), the one copy that mirrors app-api's `/workflows` routes;
 * the SPA never parses or writes the file (`references/workflow.<name>.md`, D1).
 */

import { WorkflowFilesRule, WorkflowRef } from '../services/workflows/workflow.models';

export type {
  WorkflowAgentView,
  WorkflowDocument,
  WorkflowModelView,
  WorkflowRef,
  WorkflowSheet,
  WorkflowSheetBlocker,
  WorkflowStepView,
  WorkflowSummary,
  WorkflowView,
} from '../services/workflows/workflow.models';
export type { WorkflowWrite } from '../services/workflows/workflows-api.service';

/** `WORKFLOW_RUN_MAX_STEPS`, which the parser pins too. The canvas shows it from the start. */
export const MAX_WORKFLOW_STEPS = 8;

/** `WORKFLOW_RUN_CONCURRENCY`: a stage with more steps than this runs the rest in a queue. */
export const WORKFLOW_STAGE_CONCURRENCY = 4;

/** `WORKFLOW_RUN_MIN_STEPS`: step 1's engine starts no run of fewer. */
export const MIN_WORKFLOW_STEPS = 2;

/** The two reads every step can take besides other steps' results. */
export const INPUT_READS = ['request', 'files'] as const;
export type InputRead = (typeof INPUT_READS)[number];

export type WorkflowFiles = WorkflowFilesRule;
export type WorkflowResultMode = 'card' | 'merge';

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
