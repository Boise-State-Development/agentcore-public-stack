/**
 * Stream Parser Utilities
 *
 * Shared stream parsing logic for SSE events. This module provides pure
 * parsing functions that can be used by any service that needs to handle
 * streaming responses.
 *
 * @example
 * ```typescript
 * import {
 *   processStreamEvent,
 *   createStreamLineParser,
 *   StreamParserCallbacks
 * } from '@shared/utils/stream-parser';
 *
 * const callbacks: StreamParserCallbacks = {
 *   onContentBlockDelta: (data) => {
 *     if (data.text) {
 *       this.content += data.text;
 *     }
 *   },
 *   onDone: () => {
 *     this.isComplete = true;
 *   }
 * };
 *
 * // For EventSourceMessage from fetch-event-source
 * processStreamEvent(msg.event, JSON.parse(msg.data), callbacks);
 *
 * // For raw SSE lines
 * const parser = createStreamLineParser(callbacks);
 * parser.parseLine(line);
 * ```
 */

// Core parsing functions
export {
  processStreamEvent,
  createStreamLineParser,
  inferContentBlockType,
  extractStreamingStringField,
  parseToolResultContent,
  type StreamParserCallbacks,
} from './stream-parser-core';

// Validation functions (for advanced use cases)
export {
  validateMessageStartEvent,
  validateContentBlockStartEvent,
  validateContentBlockDeltaEvent,
  validateContentBlockStopEvent,
  validateMessageStopEvent,
  validateToolUseEvent,
  validateToolResultEvent,
  validateQuotaWarningEvent,
  validateQuotaExceededEvent,
  validateConversationalStreamError,
  validateCitation,
  validateOAuthRequiredEvent,
  validateToolApprovalRequiredEvent,
  validateCompactionEvent,
  validateArtifactEvent,
  validateUiResourceEvent,
  validateToolInputPartialEvent,
} from './stream-parser-core';

// Types
export type {
  // Event types
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
  MetadataEvent,
  ReasoningEvent,
  ToolResultEventData,
  QuotaWarningEvent,
  QuotaExceededEvent,
  StreamErrorEvent,
  ConversationalStreamErrorEvent,
  OAuthRequiredEvent,
  ToolApprovalRequiredEvent,
  CompactionEvent,
  ArtifactEvent,
  UiResourceEvent,
  ToolInputPartialEvent,
  McpUiCsp,
  McpUiPermissions,
  StreamEventType,
  StreamEventData,
  ParsedStreamEvent,
  // Builder types
  ContentBlockType,
  ContentBlockBuilder,
  MessageBuilder,
  ToolResultContent,
  ToolProgress,
} from './stream-parser-types';
