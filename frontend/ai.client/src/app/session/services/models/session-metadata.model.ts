// Session Metadata Models
// Matches backend SessionMetadata and SessionPreferences models

/**
 * Display state for a single promoted visual (inline tool result).
 */
export interface VisualDisplayState {
  /** Whether the user has dismissed this visual */
  dismissed: boolean;
  /** Whether the visual is expanded (default: true) */
  expanded: boolean;
}

export interface SessionPreferences {
  lastModel?: string;
  enabledTools?: string[];
  selectedPromptId?: string;
  customPromptText?: string;
  assistantId?: string;
  /**
   * Set when the session is a task in a shared project (stamped by the
   * backend when a turn runs on the project's agent). Groups it under the
   * project in the sidebar and enables "Project members" sharing.
   */
  projectId?: string;
  /** Display state for promoted visuals, keyed by tool_use_id */
  visualState?: Record<string, VisualDisplayState>;
}

/**
 * What a conversation's paused turn is waiting on the user for. Matches the
 * backend's `AwaitingInputKind` (`apis/shared/sessions/models.py`).
 */
export type AwaitingInputKind = 'oauth' | 'tool_approval' | 'user_question' | 'browser_login';

export interface SessionMetadata {
  sessionId: string;
  userId: string;
  title: string;
  status: 'active' | 'archived' | 'deleted';
  createdAt: string;  // ISO 8601 timestamp
  lastMessageAt: string;  // ISO 8601 timestamp
  messageCount: number;
  starred?: boolean;
  tags?: string[];
  preferences?: SessionPreferences;
  /** Running USD cost across all turns in this session. Denormalized on the
   *  session row by the backend's _bump_session_aggregates; legacy sessions
   *  are lazily backfilled on first read. */
  totalCost?: number;
  /** Input tokens consumed by the most recent turn (includes system prompt + tools). */
  lastContextTokens?: number;
  /** Model context window (max input tokens) at the time of the most recent turn. */
  contextWindow?: number;
  /** Cumulative count of turns the backend has rolled into a compaction
   *  summary in this session. Drives the end-of-conversation summary
   *  indicator after a refresh. */
  totalSummarizedTurns?: number;
  /** True when the last turn ended in a recoverable max_tokens truncation.
   *  Lets the "Continue" affordance survive a page refresh. Cleared
   *  server-side at the start of any new (non-interrupt-resume) turn. */
  lastTurnContinuable?: boolean;
  /** True when the last turn was interrupted before completion (user Stop,
   *  refresh, or dropped connection). Lets the reload chip survive a
   *  refresh. Cleared server-side at the start of any new turn. */
  lastTurnInterrupted?: boolean;
  /** Why the last turn was interrupted. 'user_stopped' (deliberate Stop) →
   *  no Continue; 'connection_lost' (refresh / dropped connection) → offer
   *  Continue. */
  lastTurnInterruptReason?:
    | 'user_stopped'
    | 'navigated_away'
    | 'connection_lost'
    | 'unknown';
  /** ISO 8601 timestamp when the interruption was detected. */
  lastTurnInterruptedAt?: string;
  /** True when an unattended (scheduled) run left a response the user hasn't
   *  opened yet. Server-persisted, so the unread dot survives reload and
   *  reaches other devices; cleared server-side via POST /sessions/{id}/read
   *  when the user opens the session. Distinct from the ephemeral, in-tab
   *  unread state ChatStateService tracks for interactive background
   *  completions — the session list ORs the two. */
  unread?: boolean;
  /** Which prompt this conversation's paused turn waits on, set only while
   *  the pause can still be answered (a breadcrumb plus an unexpired
   *  `pausedTurn` snapshot). Derived server-side on every sessions surface;
   *  drives the amber "needs your input" dot in the session list, which also
   *  reads the in-tab mark ChatStateService keeps from the live stream. */
  awaitingInput?: AwaitingInputKind | null;
  /** When the user pinned this conversation. Present only on entries of the
   *  list response's `pinned` array (and on optimistic pins). */
  pinnedAt?: string;
}

// Request model for updating session metadata
export interface UpdateSessionMetadataRequest {
  title?: string;
  status?: 'active' | 'archived' | 'deleted';
  starred?: boolean;
  tags?: string[];
  lastModel?: string;
  enabledTools?: string[];
  /** Send `null` to explicitly clear the selection. Omit the field to leave
   *  the persisted value unchanged. */
  selectedPromptId?: string | null;
  customPromptText?: string;
  assistantId?: string;
}
