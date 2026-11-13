/**
 * Handoffs (docs/specs/agent-handoffs-and-consults.md §4.2–§4.3). Shapes mirror app-api's
 * `apis.shared.handoff.models` and the `/sessions/{id}/handoffs` routes.
 */

import { FolderAttachmentData } from '../models/message.model';

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
  kind: 'mention' | 'suggested' | 'send_back';
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
