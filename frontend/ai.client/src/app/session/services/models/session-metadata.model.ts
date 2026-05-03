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
  /** Display state for promoted visuals, keyed by tool_use_id */
  visualState?: Record<string, VisualDisplayState>;
}

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
}

// Request model for updating session metadata
export interface UpdateSessionMetadataRequest {
  title?: string;
  status?: 'active' | 'archived' | 'deleted';
  starred?: boolean;
  tags?: string[];
  lastModel?: string;
  enabledTools?: string[];
  selectedPromptId?: string;
  customPromptText?: string;
  assistantId?: string;
}
