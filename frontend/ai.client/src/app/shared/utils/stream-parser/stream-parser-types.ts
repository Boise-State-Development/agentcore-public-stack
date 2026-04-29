/**
 * Stream Parser Types
 *
 * Shared type definitions for SSE stream parsing used by both the main
 * StreamParserService and the PreviewChatService.
 */

import type {
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
} from '../../../session/services/models/message.model';

import type { MetadataEvent } from '../../../session/services/models/content-types';

// Re-export for convenience
export type {
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
  MetadataEvent,
};

/**
 * Quota warning event from the stream
 */
export interface QuotaWarningEvent {
  type: 'quota_warning';
  warningLevel: string;
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  remaining: number;
  message: string;
}

/**
 * Quota exceeded event from the stream
 */
export interface QuotaExceededEvent {
  type: 'quota_exceeded';
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  periodType: string;
  tierName?: string;
  resetInfo: string;
  message: string;
}

/**
 * Stream error event (structured error from backend)
 */
export interface StreamErrorEvent {
  error: string;
  code: string;
  detail?: string;
  recoverable: boolean;
  metadata?: Record<string, unknown>;
}

/**
 * Conversational stream error (displayed as assistant message)
 */
export interface ConversationalStreamErrorEvent {
  type: 'stream_error';
  code: string;
  message: string;
  recoverable: boolean;
  retry_after?: number;
  metadata?: Record<string, unknown>;
}

/**
 * Reasoning event containing chain-of-thought text
 */
export interface ReasoningEvent {
  reasoningText?: string;
}

/**
 * OAuth required event — emitted when an external MCP tool needs the user
 * to grant consent via AgentCore Identity. The agent's tool call is paused
 * (Strands interrupt) and the frontend resumes the same turn after the
 * user completes consent by POSTing back the carried `interruptId`.
 */
export interface OAuthRequiredEvent {
  type: 'oauth_required';
  providerId: string;
  authorizationUrl: string;
  interruptId: string;
}

/**
 * Tool approval required event — emitted when an MCP tool flagged
 * `needs_approval` in the catalog is about to run. The agent's tool call
 * is paused (Strands interrupt); the frontend renders an inline
 * approve/decline prompt and resumes the same turn by POSTing the carried
 * `interruptId` with `response: "approved" | "declined"`.
 */
export interface ToolApprovalRequiredEvent {
  type: 'tool_approval_required';
  interruptId: string;
  toolUseId: string;
  toolName: string;
  /** JSON-encoded tool input arguments. Pre-stringified by the backend so
   *  one shape works for both the live SSE event and the persisted
   *  PendingInterrupt breadcrumb (which DynamoDB would otherwise coerce
   *  floats inside). */
  toolInput?: string;
  message: string;
}

/**
 * Tool result event data structure
 */
export interface ToolResultEventData {
  tool_result: {
    toolUseId: string;
    content?: Array<{
      text?: string;
      json?: unknown;
      image?: {
        format?: string;
        source?: { data?: string; bytes?: string };
        data?: string;
      };
    }>;
    status?: 'success' | 'error';
  };
}

/**
 * All supported SSE event types
 */
export type StreamEventType =
  | 'message_start'
  | 'content_block_start'
  | 'content_block_delta'
  | 'content_block_stop'
  | 'tool_use'
  | 'tool_result'
  | 'message_stop'
  | 'done'
  | 'error'
  | 'metadata'
  | 'reasoning'
  | 'quota_warning'
  | 'quota_exceeded'
  | 'stream_error'
  | 'citation'
  | 'oauth_required';

/**
 * Union type of all possible event data types
 */
export type StreamEventData =
  | MessageStartEvent
  | ContentBlockStartEvent
  | ContentBlockDeltaEvent
  | ContentBlockStopEvent
  | MessageStopEvent
  | ToolUseEvent
  | ToolResultEventData
  | MetadataEvent
  | ReasoningEvent
  | QuotaWarningEvent
  | QuotaExceededEvent
  | StreamErrorEvent
  | ConversationalStreamErrorEvent
  | Citation
  | OAuthRequiredEvent
  | null
  | undefined;

/**
 * Parsed stream event with type and data
 */
export interface ParsedStreamEvent {
  type: StreamEventType;
  data: StreamEventData;
}

/**
 * Content block builder type (text or tool_use)
 */
export type ContentBlockType = 'text' | 'tool_use' | 'toolUse' | 'reasoningContent';

/**
 * Tool result content structure
 */
export interface ToolResultContent {
  text?: string;
  json?: unknown;
  image?: { format: string; data: string };
  document?: Record<string, unknown>;
}

/**
 * Internal representation of a content block being built from stream events
 */
export interface ContentBlockBuilder {
  index: number;
  type: ContentBlockType;
  textChunks: string[];
  inputChunks: string[];
  reasoningChunks: string[];
  toolUseId?: string;
  toolName?: string;
  result?: {
    content: ToolResultContent[];
    status: 'success' | 'error';
  };
  status?: 'pending' | 'complete' | 'error';
  isComplete: boolean;
}

/**
 * Internal representation of a message being built from stream events
 */
export interface MessageBuilder {
  id: string;
  role: 'user' | 'assistant';
  contentBlocks: Map<number, ContentBlockBuilder>;
  created_at: string;
  isComplete: boolean;
}

/**
 * Tool progress state for UI feedback
 */
export interface ToolProgress {
  visible: boolean;
  message?: string;
  toolName?: string;
  toolUseId?: string;
  startTime?: number;
}
