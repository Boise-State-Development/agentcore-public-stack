/**
 * TypeScript interfaces compatible with Strands SDK ContentBlock and Message objects
 * Based on Strands SDK v0.1.x type definitions
 * Source: https://strandsagents.com/0.1.x/documentation/docs/api-reference/types/
 */

// ============================================================================
// Media Types
// ============================================================================

export type DocumentFormat = 
  | 'pdf' 
  | 'csv' 
  | 'doc' 
  | 'docx' 
  | 'xls' 
  | 'xlsx' 
  | 'html' 
  | 'txt' 
  | 'md';

export type ImageFormat = 'png' | 'jpeg' | 'gif' | 'webp';

export type VideoFormat = 
  | 'flv' 
  | 'mkv' 
  | 'mov' 
  | 'mpeg' 
  | 'mpg' 
  | 'mp4' 
  | 'three_gp' 
  | 'webm' 
  | 'wmv';

export interface DocumentSource {
  bytes: Uint8Array | ArrayBuffer;
}

export interface DocumentContent {
  format: DocumentFormat;
  name: string;
  source: DocumentSource;
}

export interface ImageSource {
  bytes: Uint8Array | ArrayBuffer;
}

export interface ImageContent {
  format: ImageFormat;
  source: ImageSource;
}

export interface VideoSource {
  bytes: Uint8Array | ArrayBuffer;
}

export interface VideoContent {
  format: VideoFormat;
  source: VideoSource;
}

// ============================================================================
// Tool Types
// ============================================================================

export type ToolResultStatus = 'success' | 'error';

export interface ToolResultContent {
  document?: DocumentContent;
  image?: ImageContent;
  json?: any;
  text?: string;
}

export interface ToolResult {
  content: ToolResultContent[];
  status: ToolResultStatus;
  toolUseId: string;
}

export interface ToolUse {
  input: any;
  name: string;
  toolUseId: string;
}

// ============================================================================
// Guardrail Types
// ============================================================================

export interface GuardContentText {
  qualifiers: Array<'grounding_source' | 'query' | 'guard_content'>;
  text: string;
}

export interface GuardContent {
  text?: GuardContentText;
}

// ============================================================================
// Reasoning Types
// ============================================================================

export interface ReasoningTextBlock {
  signature?: string;
  text: string;
}

export interface ReasoningContentBlock {
  reasoningText?: ReasoningTextBlock;
  redactedContent?: Uint8Array | ArrayBuffer;
}

// ============================================================================
// Cache Point
// ============================================================================

export interface CachePoint {
  type: string; // typically "default"
}

// ============================================================================
// Content Block (Main Type)
// ============================================================================

export interface ContentBlock {
  cachePoint?: CachePoint;
  document?: DocumentContent;
  guardContent?: GuardContent;
  image?: ImageContent;
  reasoningContent?: ReasoningContentBlock;
  text?: string;
  toolResult?: ToolResult;
  toolUse?: ToolUse;
  video?: VideoContent;
}

// ============================================================================
// Message Types
// ============================================================================

export type Role = 'user' | 'assistant';

export interface Message {
  id?: string;
  content: ContentBlock[];
  role: Role;
}

export type Messages = Message[];

// ============================================================================
// System Content Block
// ============================================================================

export interface SystemContentBlock {
  guardContent?: GuardContent;
  text: string;
}

// ============================================================================
// Streaming Types
// ============================================================================

export type StopReason = 
  | 'content_filtered' 
  | 'end_turn' 
  | 'guardrail_intervened' 
  | 'max_tokens' 
  | 'stop_sequence' 
  | 'tool_use';

export interface ContentBlockDeltaText {
  text: string;
}

export interface ContentBlockDeltaToolUse {
  input: string;
}

export interface ReasoningContentBlockDelta {
  redactedContent?: Uint8Array | ArrayBuffer;
  signature?: string;
  text?: string;
}

export interface ContentBlockDelta {
  reasoningContent?: ReasoningContentBlockDelta;
  text?: string;
  toolUse?: ContentBlockDeltaToolUse;
}

export interface ContentBlockDeltaEvent {
  contentBlockIndex?: number;
  delta: ContentBlockDelta;
}

export interface ContentBlockStartToolUse {
  name: string;
  toolUseId: string;
}

export interface ContentBlockStart {
  toolUse?: ContentBlockStartToolUse;
}

export interface ContentBlockStartEvent {
  contentBlockIndex?: number;
  start: ContentBlockStart;
}

export interface ContentBlockStopEvent {
  contentBlockIndex?: number;
}

export interface MessageStartEvent {
  role: Role;
}

export interface MessageStopEvent {
  additionalModelResponseFields?: any;
  stopReason: StopReason;
}

export interface Usage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadInputTokens?: number;
  cacheWriteInputTokens?: number;
}

export interface Metrics {
  latencyMs: number;
  timeToFirstByteMs?: number;
}

export interface MetadataEvent {
  metrics?: Metrics;
  trace?: any;
  usage?: Usage;
}

export interface ExceptionEvent {
  message: string;
}

export interface ModelStreamErrorEvent extends ExceptionEvent {
  originalMessage: string;
  originalStatusCode: number;
}

export interface RedactContentEvent {
  redactUserContentMessage?: string;
  redactAssistantContentMessage?: string;
}

export interface StreamEvent {
  contentBlockDelta?: ContentBlockDeltaEvent;
  contentBlockStart?: ContentBlockStartEvent;
  contentBlockStop?: ContentBlockStopEvent;
  internalServerException?: ExceptionEvent;
  messageStart?: MessageStartEvent;
  messageStop?: MessageStopEvent;
  metadata?: MetadataEvent;
  modelStreamErrorException?: ModelStreamErrorEvent;
  serviceUnavailableException?: ExceptionEvent;
  throttlingException?: ExceptionEvent;
  validationException?: ExceptionEvent;
}

// ============================================================================
// Utility Types
// ============================================================================

/**
 * Helper type to create a new message
 */
export function createMessage(role: Role, content: ContentBlock[]): Message {
  return { role, content };
}

/**
 * Helper type to create a text content block
 */
export function createTextContent(text: string): ContentBlock {
  return { text };
}

/**
 * Helper type to create an image content block
 */
export function createImageContent(
  format: ImageFormat, 
  bytes: Uint8Array | ArrayBuffer
): ContentBlock {
  return {
    image: {
      format,
      source: { bytes }
    }
  };
}

/**
 * Helper type to create a document content block
 */
export function createDocumentContent(
  format: DocumentFormat,
  name: string,
  bytes: Uint8Array | ArrayBuffer
): ContentBlock {
  return {
    document: {
      format,
      name,
      source: { bytes }
    }
  };
}

/**
 * Helper type to create a tool use content block
 */
export function createToolUse(
  name: string,
  toolUseId: string,
  input: any
): ContentBlock {
  return {
    toolUse: {
      name,
      toolUseId,
      input
    }
  };
}

/**
 * Helper type to create a tool result content block
 */
export function createToolResult(
  toolUseId: string,
  status: ToolResultStatus,
  content: ToolResultContent[]
): ContentBlock {
  return {
    toolResult: {
      toolUseId,
      status,
      content
    }
  };
}