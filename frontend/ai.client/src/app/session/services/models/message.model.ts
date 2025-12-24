// models/message.model.ts
// ============================================================================
// Core Message and ContentBlock Types
// Matches the backend API MessageResponse and MessageContent models
// ============================================================================

/**
 * Tool result content types
 */
export interface ToolResultContent {
  text?: string;
  json?: unknown;
  image?: {
    format: string;
    data: string;
  };
  document?: Record<string, unknown>;
}

/**
 * Reasoning content structure (extended thinking from Claude 3.7+, GPT, etc.)
 * Matches the Bedrock Converse API ReasoningContentBlock format
 */
export interface ReasoningContentData {
  reasoningText?: {
    text: string;
    /** Signature for verification (required for subsequent Bedrock API calls) */
    signature?: string;
  };
  /** Encrypted reasoning content for trust and safety */
  redactedContent?: string;
}

/**
 * Tool use data structure
 */
export interface ToolUseData {
  toolUseId: string;
  name: string;
  input: Record<string, unknown>;
  /** Tool result - populated when tool execution completes */
  result?: {
    content: ToolResultContent[];
    status: 'success' | 'error';
  };
  /** Tool execution status */
  status?: 'pending' | 'complete' | 'error';
}

/**
 * Content block in a message.
 * Matches the backend MessageContent model.
 */
export interface ContentBlock {
  /** Content type (text, toolUse, toolResult, image, document, reasoningContent, etc.) */
  type: string;
  /** Text content (if type is text) */
  text?: string | null;
  /** Tool use information (if type is toolUse) - now includes result */
  toolUse?: ToolUseData | Record<string, unknown> | null;
  /** Tool execution result (if type is toolResult) - deprecated, use toolUse.result instead. Kept for backwards compatibility with API responses. */
  toolResult?: Record<string, unknown> | null;
  /** Image content (if type is image) */
  image?: Record<string, unknown> | null;
  /** Document content (if type is document) */
  document?: Record<string, unknown> | null;
  /** Reasoning content (if type is reasoningContent) - extended thinking from Claude 3.7+, GPT, etc. */
  reasoningContent?: ReasoningContentData | null;
}

/**
 * Message model matching the backend API MessageResponse.
 * This is the canonical Message type used throughout the application.
 */
export interface Message {
  /** Unique identifier for the message */
  id: string;
  /** Role of the message sender */
  role: 'user' | 'assistant' | 'system';
  /** List of content blocks in the message */
  content: ContentBlock[];
  /** ISO timestamp when the message was created */
  created_at?: string;
  /** Optional metadata associated with the message */
  metadata?: Record<string, unknown> | null;
}

// ============================================================================
// Type Guards and Helpers
// ============================================================================

/**
 * Type guard to check if a content block is a text block
 */
export function isTextContentBlock(block: ContentBlock): block is ContentBlock & { type: 'text'; text: string } {
  return block.type === 'text' && block.text !== null && block.text !== undefined;
}

/**
 * Type guard to check if a content block is a tool use block
 */
export function isToolUseContentBlock(block: ContentBlock): block is ContentBlock & { type: 'toolUse' | 'tool_use'; toolUse: Record<string, unknown> } {
  return (block.type === 'toolUse' || block.type === 'tool_use') && block.toolUse !== null && block.toolUse !== undefined;
}

/**
 * Type guard to check if a content block is a reasoning content block
 */
export function isReasoningContentBlock(block: ContentBlock): block is ContentBlock & { type: 'reasoningContent'; reasoningContent: ReasoningContentData } {
  return block.type === 'reasoningContent' && block.reasoningContent !== null && block.reasoningContent !== undefined;
}


// ============================================================================
// SSE Event Types
// ============================================================================

export interface MessageStartEvent {
  role: 'user' | 'assistant';
  // Note: Message ID is no longer sent by server, computed client-side as msg-{sessionId}-{index}
}

/**
 * Event emitted at the start of a content block.
 *
 * NOTE: According to AWS ConverseStream API:
 * - contentBlockStart is OPTIONAL for text blocks (Claude skips it entirely)
 * - contentBlockStart is REQUIRED for tool_use blocks (contains toolUseId and name)
 * - Some providers (like Gemini) emit contentBlockStart without type for text blocks
 */
export interface ContentBlockStartEvent {
  contentBlockIndex: number;
  /** Type is optional - defaults to 'text' if not specified */
  type?: 'text' | 'tool_use' | 'tool_result' | 'toolUse' | 'toolResult';
  toolUse?: {
    toolUseId: string;
    name: string;
    type: 'tool_use';
  };
}

/**
 * Event emitted for content block deltas (incremental updates).
 *
 * NOTE: Type can be inferred from content:
 * - If 'text' field is present -> type is 'text'
 * - If 'input' field is present -> type is 'tool_use'
 */
export interface ContentBlockDeltaEvent {
  contentBlockIndex: number;
  /** Type is optional - can be inferred from text/input fields */
  type?: 'text' | 'tool_use' | 'tool_result' | 'toolUse' | 'toolResult';
  text?: string;
  input?: string;
}

export interface ContentBlockStopEvent {
  contentBlockIndex: number;
}

export interface MessageStopEvent {
  stopReason: string;
}

export interface ToolUseEvent {
  tool_use: {
    name: string;
    tool_use_id: string;
    input: string;
  };
}