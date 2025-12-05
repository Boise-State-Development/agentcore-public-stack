// models/message.model.ts
// ============================================================================
// Core Message and ContentBlock Types
// Matches the backend API MessageResponse and MessageContent models
// ============================================================================

/**
 * Content block in a message.
 * Matches the backend MessageContent model.
 */
export interface ContentBlock {
  /** Content type (text, toolUse, toolResult, image, document, etc.) */
  type: string;
  /** Text content (if type is text) */
  text?: string | null;
  /** Tool use information (if type is toolUse) */
  toolUse?: Record<string, unknown> | null;
  /** Tool execution result (if type is toolResult) */
  toolResult?: Record<string, unknown> | null;
  /** Image content (if type is image) */
  image?: Record<string, unknown> | null;
  /** Document content (if type is document) */
  document?: Record<string, unknown> | null;
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
 * Type guard to check if a content block is a tool result block
 */
export function isToolResultContentBlock(block: ContentBlock): block is ContentBlock & { type: 'toolResult' | 'tool_result'; toolResult: Record<string, unknown> } {
  return (block.type === 'toolResult' || block.type === 'tool_result') && block.toolResult !== null && block.toolResult !== undefined;
}

// ============================================================================
// Legacy Type Aliases (for backward compatibility with streaming)
// ============================================================================

/**
 * @deprecated Use ContentBlock instead. This is kept for backward compatibility with streaming code.
 */
export type TextContentBlock = ContentBlock & { type: 'text'; text: string };

/**
 * @deprecated Use ContentBlock instead. This is kept for backward compatibility with streaming code.
 */
export type ToolUseContentBlock = ContentBlock & { type: 'toolUse' | 'tool_use'; toolUse: Record<string, unknown> };

/**
 * @deprecated Use ContentBlock instead. This is kept for backward compatibility with streaming code.
 */
export type ToolResultContentBlock = ContentBlock & { type: 'toolResult' | 'tool_result'; toolResult: Record<string, unknown> };

/**
 * @deprecated Use ContentBlock instead. This is kept for backward compatibility.
 */
export type ContentBlockType = 'text' | 'tool_use' | 'tool_result' | 'toolUse' | 'toolResult';

// ============================================================================
// SSE Event Types
// ============================================================================

export interface MessageStartEvent {
  role: 'user' | 'assistant';
}

export interface ContentBlockStartEvent {
  contentBlockIndex: number;
  type: ContentBlockType;
  toolUse?: {
    toolUseId: string;
    name: string;
    type: 'tool_use';
  };
}

export interface ContentBlockDeltaEvent {
  contentBlockIndex: number;
  type: ContentBlockType;
  text?: string;
  input?: string;
}

export interface ContentBlockStopEvent {
  contentBlockIndex: number;
}

export interface MessageStopEvent {
  stopReason: string;
  message_id?: string;
}

export interface ToolUseEvent {
  tool_use: {
    name: string;
    tool_use_id: string;
    input: string;
  };
}