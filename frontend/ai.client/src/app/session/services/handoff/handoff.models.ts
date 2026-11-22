/**
 * Handoffs (docs/specs/agent-handoffs-and-consults.md §4.2–§4.3). Shapes mirror app-api's
 * `apis.shared.handoff.models` and the `/sessions/{id}/handoffs` routes.
 */

import { FolderAttachmentData } from '../models/message.model';
import type { DeliveryPartStatus } from './handoff-prompt';

export type HandoffStatus =
  | 'open'
  | 'running'
  | 'needs_input'
  | 'done'
  | 'stopped'
  | 'dismissed'
  | 'abandoned';

export type WaitingKind = 'oauth_consent' | 'tool_approval' | 'user_question' | 'browser_login';

/** What a `needs_input` handoff is waiting on. Never request or conversation text. */
export interface WaitingOn {
  kind: WaitingKind;
  name: string;
}

/** The breadcrumb kinds a source can relay (`apis.shared.handoff.relay`). */
export type RelayedPromptKind = 'oauth' | 'tool_approval' | 'user_question' | 'browser_login';

/**
 * One prompt a paused target is waiting on, as its source relays it (handoffs spec §4.3, PR-0.3c).
 * The target's `PendingInterrupt` breadcrumb in its wire names, minus the target's message id and a
 * sign-in's browser session: the relay renders it with the prompt the SPA already has for its kind.
 */
export interface RelayedPrompt {
  interruptId: string;
  kind: RelayedPromptKind;
  providerId?: string;
  toolUseId?: string;
  toolName?: string;
  /** JSON-encoded, as on the breadcrumb. */
  toolInput?: string;
  message?: string;
  /** JSON-encoded, as on the breadcrumb. */
  questions?: string;
}

export interface TurnsCovered {
  first: number;
  last: number;
  of: number;
  omitted: number;
}

/** One handoff as the source thread sees it: `GET .../handoffs/{id}` and `handoffs` on `GET /messages`. */
export interface HandoffView {
  handoffId: string;
  sessionId: string;
  /**
   * `prompt` is a workflow run's prompt step (workflow-designer §21.3), `checklist` its checklist
   * step (§22.3): no Agent, no conversation.
   */
  kind: 'mention' | 'suggested' | 'send_back' | 'prompt' | 'checklist';
  status: HandoffStatus;
  agentId: string;
  agentName: string;
  request: string;
  /** The handoff row sits after the message with this index. */
  sourceMessageIndex: number;
  excerptTokens: number;
  turnsCovered?: TurnsCovered;
  createdAt: string;
  updatedAt: string;
  targetSessionId?: string;
  /** The latest tool summary when the target's turn ended. */
  statusLine?: string;
  waitingOn?: WaitingOn;
  stoppedReason?: string;
  /** A `needs_input` handoff's prompts, read from its target. Never on any other status. */
  relayedPrompts?: RelayedPrompt[];
  /** Set on a workflow run's step (workflow-runs spec §3.1). */
  runId?: string;
  stepId?: string;
  /** A suggestion's chip (`suggest_handoff`, §4.4): what the model offered. */
  title?: string;
  tldr?: string;
  /** The Agent name as the model wrote it, unresolved; matched by `matchSuggestedAgent`. */
  agentHint?: string;
  /** The `suggest_handoff` call, which places the chip under the message holding it. */
  toolUseId?: string;
  /** A prompt or checklist step's model (§21.3). Its `agentName` is the model's display name and `agentId` is empty. */
  modelId?: string;
  /** A checklist step's field sets, in the order it runs them (§22.3). */
  fieldSets?: string[];
  /** A done prompt step's answer, as stored (capped at write, with the cut-off marker when cut). */
  outputText?: string;
  outputTokens?: number;
}

/** Why a prompt step stopped (`stoppedReason` on its row, workflow-designer §21.3). */
export type PromptStopReason = 'user_stopped' | 'too_large' | 'model_refused' | 'quota' | 'timeout' | 'error';

/**
 * A workflow run's model step: a prompt step (one model call, §21.3) or a checklist step (one call
 * per field set, §22.3), each with no Agent and no conversation.
 */
export function isModelStep(row: Pick<HandoffView, 'kind'> | null | undefined): boolean {
  return row?.kind === 'prompt' || row?.kind === 'checklist';
}

/**
 * A suggestion the user hasn't sent yet: rendered as a chip under its message, not as a row.
 * Once sent it has a target and becomes an ordinary handoff row.
 */
export function isSuggestionChip(row: HandoffView): boolean {
  return row.kind === 'suggested' && !row.targetSessionId;
}

/** `POST /sessions/{id}/handoffs`: the card to review and the exact prompt it makes. */
export interface CreateHandoffResponse {
  handoffId: string;
  excerpt: string;
  excerptHash: string;
  excerptTokens: number;
  turnsCovered?: TurnsCovered;
  contextBlock: string;
  prompt: string;
  request: string;
  /** The handoff row sits after the message with this index. */
  sourceMessageIndex: number;
  sourceTitle: string;
}

/** `spawnedFrom` on a target's `GET /messages`: where its context came from. */
export interface SpawnedFrom {
  sessionId: string;
  handoffId: string;
  sourceTitle: string;
  agentName: string;
}

/** What the user mentioned and typed, carried from the composer into the review sheet. */
export interface HandoffDraft {
  agentId: string;
  agentName: string;
  request: string;
  fileUploadIds?: string[];
  invokedSkillIds?: string[];
  folders?: FolderAttachmentData[];
  /**
   * The source conversation's bound Agent, if any: the brief is requested with the body a turn
   * there would carry, so the Runtime builds the same agent and reads the source's prompt cache.
   */
  sourceAssistantId?: string;
  /**
   * Set when the sheet was opened from a suggestion chip's Start (§4.4): the suggested row to
   * start, instead of opening a new mention. Cancel then leaves the chip in place.
   */
  suggestionId?: string;
}

/** Why no brief was written (`handoff_brief_fallback`); the card keeps the excerpt. */
export type BriefFallbackReason =
  | 'flag_off'
  | 'not_open'
  | 'busy'
  | 'preempted'
  | 'quota'
  | 'refused'
  | 'timeout'
  | 'error'
  | 'tool_use_only'
  | 'short_source'
  | 'history_ends_on_user';

/**
 * The brief the source's own model writes for the card (handoffs spec §4.5.1).
 * `writing` until the stream ends; `written` puts it in the card; `fallback` keeps the excerpt.
 */
export interface HandoffBrief {
  status: 'writing' | 'written' | 'fallback';
  /** The text so far while writing; the whole brief once written. Empty on a fallback. */
  text: string;
  briefHash?: string;
  cacheStatus?: string;
  fallbackReason?: BriefFallbackReason;
}

/** The review sheet's state for one source conversation. */
export interface HandoffReview {
  draft: HandoffDraft;
  phase: 'loading' | 'ready' | 'sending' | 'error';
  error?: string;
  created?: CreateHandoffResponse;
  /** The request as it will be sent (editable). */
  request: string;
  /** The context card as it will be sent (editable). */
  card: string;
  cardRemoved: boolean;
  /** Present when a brief was requested (`features.handoffBrief`). */
  brief?: HandoffBrief;
  /** The source conversation's files the sheet lists (§4.9); absent until the listing lands. */
  files?: ShareableFile[];
  /** The listed files the user is sharing, in list order. */
  fileIds: string[];
}

/** A file the review sheet can share, as the source's file listing returned it. */
export interface ShareableFile {
  uploadId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
  /** Set when the source's file is itself shared from elsewhere: the line names its original. */
  linkedFrom?: { sessionId: string; uploadId: string; fromTitle: string } | null;
}

/** Files one handoff or one delivery shares (`HANDOFF_MAX_FILES` / `DELIVERY_MAX_FILES`). */
export const MAX_SHARED_FILES = 5;

/** User turns whose attachments the review sheet ticks by default (§4.9). */
export const SHARED_FILES_RECENT_TURNS = 5;

/** A reference to an artifact the sending conversation made, pinned at the version sent (§4.8). */
export interface DeliveryArtifact {
  artifactId: string;
  version: number;
  title: string;
  contentType: string;
}

/**
 * A file a card or a handoff shares by reference (§4.9): always the original, and where it lives.
 * The receiving conversation reads it through a reference row (`referenceUploadId`), or, for a
 * file handed back to the conversation it came from, through the original itself.
 */
export interface DeliveryFile {
  uploadId: string;
  sessionId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}

/** One `<file …/>` line: the id the receiving model reads, and what the file is. */
export interface FileLine {
  uploadId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}

/**
 * A card waiting on a conversation's composer (`deliveries` on `GET /messages`, handoffs spec §4.8):
 * text from another of the user's conversations that the next send there carries.
 */
export interface DeliveryView {
  deliveryId: string;
  fromSessionId: string;
  fromTitle: string;
  handoffId?: string;
  origin: 'user' | 'agent';
  text: string;
  artifacts: DeliveryArtifact[];
  /** Files the card shares by reference (§4.9). Absent from rows written before PR-0.4a. */
  files?: DeliveryFile[];
  createdAt: string;
  /** Set on a workflow run's one result (workflow-runs spec §7, PR-W.2). */
  runId?: string;
  /**
   * A run result's parts, one per kept step in step order. The row's own `text`, `artifacts` and
   * `files` are empty then. Absent (not empty) on a plain card.
   */
  parts?: DeliveryPart[];
}

/** One step's part of a run result. Only a `done` part carries text, artifacts and files. */
export interface DeliveryPart {
  stepId: string;
  /** A prompt step's part: its model's name. */
  agentName: string;
  /** Empty on a prompt step's part, which comes from no conversation. */
  fromSessionId: string;
  /** The step conversation's title; empty on a part with no result. */
  fromTitle: string;
  status: DeliveryPartStatus;
  text: string;
  artifacts: DeliveryArtifact[];
  files: DeliveryFile[];
  /**
   * `prompt` for a prompt step's part (workflow-designer §21.3): text alone, from no conversation,
   * with its step's name as `fromTitle`. `checklist` for a checklist step's part (§22.3), the same
   * text-only shape. Absent on an Agent step's part.
   */
  kind?: 'prompt' | 'checklist';
}

/** `POST /sessions/{targetId}/handoffs/send-back`: a target's answer, offered back to its source. */
export interface CreateSendBackResponse {
  handoffId: string;
  sourceSessionId: string;
  sourceTitle: string;
  /** The sending conversation's title, which the card is labelled with. */
  fromTitle: string;
  messageIndex: number;
  excerpt: string;
  excerptHash: string;
  artifacts: DeliveryArtifact[];
  /** The target's own files, newest first, offered with the result (§4.9). */
  files?: DeliveryFile[];
  /** Present when the delivery was made at once (Bring back result). */
  deliveryId?: string;
}

/** `POST /sessions/{targetId}/handoffs/{handoffId}/deliver`: Send to *{title}* made its card. */
export interface DeliverSendBackResponse {
  deliveryId: string;
  sourceSessionId: string;
  sourceTitle: string;
}

/** Send to *{title}*'s review sheet, in the target conversation (§4.3). */
export interface SendBackReview {
  messageIndex: number;
  phase: 'loading' | 'ready' | 'sending' | 'error';
  error?: string;
  created?: CreateSendBackResponse;
  /** The card as it will be sent (editable). */
  text: string;
  /** The offered artifacts the user is sending (unticking one leaves it behind). */
  artifactIds: string[];
  /** The offered files the user is sending (§4.9). */
  fileIds: string[];
}

/** One delivery block read back out of a user message the model received (the sent bubble's card). */
export interface DeliveryBlock {
  fromTitle: string;
  text: string;
  artifacts: DeliveryArtifact[];
  /** The block's `<file …/>` lines (§4.9). */
  files: FileLine[];
}
