/**
 * Workflow runs, step 1 (docs/specs/workflow-runs.md §4, §9, §19 PR-W.1 "Built"). Shapes mirror
 * app-api's `apis.shared.handoff.runs` and `/sessions/{id}/workflow-runs`.
 *
 * A step is an ordinary `mention` handoff row with a `runId` and `stepId`, so once it starts it is
 * a `HandoffView` like any other. These types cover what is new: the run, its sheet, and its queue.
 */

import { WorkflowDocument, WorkflowRef, WorkflowRunSource } from '../../../services/workflows/workflow.models';
import { FolderAttachmentData } from '../models/message.model';
import { HandoffBrief, HandoffView, ShareableFile, TurnsCovered } from './handoff.models';

/** Agents in one run (`WORKFLOW_RUN_MAX_STEPS`). The composer refuses a ninth mention. */
export const MAX_RUN_STEPS = 8;

/** Steps started at once when the run row carries no limits (`WORKFLOW_RUN_CONCURRENCY`). */
export const DEFAULT_RUN_CONCURRENCY = 4;

export type WorkflowRunStatus = 'open' | 'approved' | 'dismissed' | 'stopped' | 'abandoned';

export interface WorkflowRunLimits {
  maxSteps: number;
  concurrency: number;
}

/** A step as the run holds it, with its handoff row's current view (`null` when the row is gone). */
export interface WorkflowRunStepView {
  stepId: string;
  handoffId: string;
  agentId: string;
  agentName: string;
  request: string;
  removed: boolean;
  /** The saved workflow's name for this step (PR-D.2); absent for a run of mentions. */
  stepName?: string;
  /** The Agent's published version when a saved workflow's run was created (PR-D.2). */
  agentVersion?: number;
  handoff?: HandoffView | null;
}

/** `GET …/workflow-runs/{runId}` and each entry of `workflowRuns` on `GET /messages`. */
export interface WorkflowRunView {
  runId: string;
  sessionId: string;
  status: WorkflowRunStatus;
  request: string;
  steps: WorkflowRunStepView[];
  fileIds: string[];
  limits: WorkflowRunLimits;
  sourceMessageIndex: number;
  createdAt: string;
  updatedAt: string;
  approvedAt?: string;
  resultDeliveryId?: string;
  /** The card as the user reviewed it, kept at approval (PR-W.4b); absent on runs approved before. */
  card?: WorkflowRunCard;
  /** The saved workflow it was started from (PR-D.2); absent for a run of mentions. */
  workflow?: WorkflowRunSource;
}

/**
 * The context card every step of a run is sent with, as reviewed: its text (normalized and capped
 * as the Runtime caps a card), or removed. On `…/approve`, and on the run row from then on.
 */
export interface WorkflowRunCard {
  text?: string;
  removed: boolean;
}

/** One step of `POST /sessions/{id}/workflow-runs`, with what pricing its first turn needs. */
export interface CreatedRunStep {
  stepId: string;
  handoffId: string;
  agentId: string;
  agentName: string;
  request: string;
  prompt: string;
  /** The Agent's own model. Absent when it names none: the step runs on the send's model. */
  modelId?: string;
  /** `modelId`'s catalog input rate, USD per million tokens. */
  inputPricePerMillionTokens?: number;
}

/** `POST /sessions/{id}/workflow-runs`: the run, the one card every step gets, and each step. */
export interface CreateWorkflowRunResponse {
  run: WorkflowRunView;
  excerpt: string;
  excerptHash: string;
  excerptTokens: number;
  turnsCovered?: TurnsCovered;
  contextBlock: string;
  sourceMessageIndex: number;
  sourceTitle: string;
  steps: CreatedRunStep[];
}

/** One Agent the composer mentioned, in the order the user mentioned them. */
export interface RunDraftAgent {
  agentId: string;
  agentName: string;
  emoji?: string;
  iconUrl?: string;
}

/** What the user mentioned and typed, carried from the composer into the run sheet. */
export interface RunDraft {
  agents: RunDraftAgent[];
  /** What the user typed, minus every mention. */
  request: string;
  fileUploadIds?: string[];
  invokedSkillIds?: string[];
  folders?: FolderAttachmentData[];
  /** The source conversation's bound Agent: the brief is asked with a turn's own body (§4.5.1). */
  sourceAssistantId?: string;
  /**
   * A saved workflow's sheet (workflow-designer.md §8.3, PR-D.2): the run is created from the
   * workflow's file on the server, not from `agents`, which only name the steps while it loads.
   */
  workflow?: RunDraftWorkflow;
}

/** The saved workflow a sheet starts, as `GET /workflows/{skillId}/{name}` gave it. */
export interface RunDraftWorkflow {
  ref: WorkflowRef;
  document: WorkflowDocument;
}

/** One step on the sheet. */
export interface RunReviewStep {
  stepId: string;
  handoffId: string;
  agentId: string;
  agentName: string;
  /** This step's own request, once its Edit was used; `null` while it follows the shared one. */
  ownRequest: string | null;
  removed: boolean;
  modelId?: string;
  inputPricePerMillionTokens?: number;
  /** A saved workflow's step (PR-D.2): its name in the file and its Agent as the starter sees it. */
  workflowStep?: RunReviewWorkflowStep;
}

/** What the sheet says about one of a saved workflow's steps (§10, §11). */
export interface RunReviewWorkflowStep {
  name: string;
  /** Other steps' names this step reads (from D.5a; none can be started before then). */
  readsSteps: string[];
  /** The starter can't use the Agent: Start stays off until the step is removed. */
  unavailable: boolean;
  /** The Agent's published version moved since the step recorded it. */
  changed: boolean;
  publishedVersion?: number;
  /** The starter owns the Agent, so their run uses its draft. */
  runsDraft: boolean;
}

/**
 * The run sheet's state for one source conversation (at most one per thread).
 *
 * `blocked` is a saved workflow's sheet before its run exists: a step the starter can't run is
 * listed with why, and the run is created once every such step is removed (§20.1).
 */
export interface RunReview {
  draft: RunDraft;
  phase: 'loading' | 'blocked' | 'ready' | 'starting' | 'error';
  error?: string;
  created?: CreateWorkflowRunResponse;
  /** The shared request, every step's until it has its own. */
  request: string;
  steps: RunReviewStep[];
  /** The one card every step receives (editable). */
  card: string;
  cardRemoved: boolean;
  brief?: HandoffBrief;
  files?: RunShareableFile[];
  /** The listed files every step shares, in list order. */
  fileIds: string[];
  /** A saved workflow's sheet: the document as it now stands (Mark as reviewed rewrites it). */
  workflow?: RunDraftWorkflow;
  /** Steps of the saved workflow removed before its run was created, with what read them. */
  removedSteps?: string[];
  /** The step whose Mark as reviewed is being saved. */
  reviewingStep?: string;
}

/** A file the run sheet lists, with the token count `GET /files` gives a prepared document. */
export interface RunShareableFile extends ShareableFile {
  /** Model-neutral (`facts_tokens` with no model); `null` until prepared, and for a reference row. */
  estimatedTokens?: number | null;
  pages?: number | null;
}

/**
 * What every step of an approved run is sent with, held by the queue. A reload rebuilds it from
 * the run row (its files and its card as reviewed), so any browser can start the steps left.
 */
export interface RunEnvelope {
  runId: string;
  sourceSessionId: string;
  sourceTitle: string;
  sourceMessageIndex: number;
  excerptTokens: number;
  turnsCovered?: TurnsCovered;
  /** The card as reviewed; absent when the user removed it. */
  contextText?: string;
  fileIds: string[];
  filesUnticked: number;
  fileUploadIds?: string[];
  invokedSkillIds?: string[];
  folders?: FolderAttachmentData[];
  concurrency: number;
}

/** One step the queue starts. */
export interface RunStepLaunch {
  stepId: string;
  handoffId: string;
  agentId: string;
  agentName: string;
  request: string;
}
