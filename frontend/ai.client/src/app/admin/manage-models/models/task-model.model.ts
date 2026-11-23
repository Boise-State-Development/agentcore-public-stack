/**
 * Task models: the models the platform runs outside the chat turn (titles, tool
 * summaries, compaction, extraction). Mirrors `apis/app_api/admin/task_models/routes.py`.
 *
 * The backend decides which models may fill a task (`eligibleModels`) and which effort
 * levels each offers; the SPA only renders those lists.
 */

export type TaskModelSlotId =
  | 'session_title'
  | 'tool_summary'
  | 'compaction_summary'
  | 'document_extract';

export interface TaskModelOption {
  modelId: string;
  modelName: string;
  providerName: string;
  /** Low to high. Empty when the model has no effort control. */
  effortLevels: string[];
  /** The row's own default effort, used when the assignment names none. */
  effortDefault: string | null;
  inputPricePerMillionTokens: number;
  outputPricePerMillionTokens: number;
}

export interface TaskModelRef {
  modelId: string;
  modelName: string | null;
  effort?: string | null;
}

export interface TaskModelAssignment extends TaskModelRef {
  updatedAt: string | null;
  updatedBy: string | null;
}

export interface TaskModelEffective extends TaskModelRef {
  /** `assigned` when an admin's choice runs, `default` otherwise. */
  source: 'assigned' | 'default';
}

export interface TaskModelSlot {
  slot: TaskModelSlotId;
  title: string;
  /** What the task does. */
  description: string;
  /** What kind of model to pick for it. */
  guidance: string;
  /** False when a call writes no cost row: its spend counts against no user's quota. */
  metered: boolean;
  /** False when the feature that runs this task is off in this deployment. */
  available: boolean;
  /** `code` for a default in code, `deploy` for one set at deploy time. */
  defaultSource: 'code' | 'deploy';
  defaultModel: TaskModelRef | null;
  /** The retirement watch: set when the default is uncataloged, retired, or unset. */
  defaultNotice: string | null;
  assignment: TaskModelAssignment | null;
  /** Set when the assignment no longer passes its checks, so the default runs instead. */
  assignmentProblem: string | null;
  effective: TaskModelEffective;
  eligibleModels: TaskModelOption[];
}

export interface TaskModelsListResponse {
  slots: TaskModelSlot[];
}

export interface TaskModelUpdate {
  /** A catalog model id, or null to clear the assignment and run the default. */
  modelId: string | null;
  /** One of the model's effort levels, or null for the model's own default. */
  effort: string | null;
}
