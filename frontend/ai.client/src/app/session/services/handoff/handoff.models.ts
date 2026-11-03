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
}
