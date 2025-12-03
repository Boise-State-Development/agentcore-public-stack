// models/message.model.ts
// ============================================================================
// Core Message and ContentBlock Types (Strands SDK Compatible)
// ============================================================================

export type ContentBlockType = 'text' | 'tool_use' | 'tool_result';

export interface TextContentBlock {
  type: 'text';
  text: string;
}

export interface ToolUseContentBlock {
  type: 'tool_use';
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResultContentBlock {
  type: 'tool_result';
  tool_use_id: string;
  content: string;
}

export type ContentBlock = TextContentBlock | ToolUseContentBlock | ToolResultContentBlock;

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: ContentBlock[];
  stopReason?: string;
  metadata?: {
    usage?: {
      inputTokens: number;
      outputTokens: number;
    };
  };
}

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
}

export interface ToolUseEvent {
  tool_use: {
    name: string;
    tool_use_id: string;
    input: string;
  };
}