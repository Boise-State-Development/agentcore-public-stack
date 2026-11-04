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
}
