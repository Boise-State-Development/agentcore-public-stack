/**
 * Stream Parser Core
 *
 * Pure parsing functions for SSE stream events. This module contains no Angular
 * dependencies or state management - it's designed to be used by services that
 * provide their own state management via callbacks.
 *
 * Usage:
 * ```typescript
 * const callbacks: StreamParserCallbacks = {
 *   onMessageStart: (data) => { ... },
 *   onContentDelta: (data) => { ... },
 *   // ... other callbacks
 * };
 *
 * // For raw SSE lines
 * const parser = createStreamLineParser(callbacks);
 * parser.parseLine(line);
 *
 * // For pre-parsed EventSourceMessage
 * processStreamEvent('content_block_delta', data, callbacks);
 * ```
 */

import type {
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
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
  ToolProgress,
} from './stream-parser-types';
import type { MetadataEvent } from '../../../session/services/models/content-types';

// =============================================================================
// Callbacks Interface
// =============================================================================

/**
 * Callbacks for handling parsed stream events.
 *
 * Consumers implement these callbacks to receive parsed events and manage
 * their own state. All callbacks are optional - only implement what you need.
 */
export interface StreamParserCallbacks {
  // Message lifecycle
  onMessageStart?: (data: MessageStartEvent) => void;
  onMessageStop?: (data: MessageStopEvent) => void;
  onDone?: () => void;

  // Content blocks
  onContentBlockStart?: (data: ContentBlockStartEvent) => void;
  onContentBlockDelta?: (data: ContentBlockDeltaEvent) => void;
  onContentBlockStop?: (data: ContentBlockStopEvent) => void;

  // Tool events
  onToolUse?: (data: ToolUseEvent) => void;
  onToolResult?: (data: ToolResultEventData) => void;
  onToolProgress?: (progress: ToolProgress) => void;

  // Metadata and auxiliary events
  onMetadata?: (data: MetadataEvent) => void;
  onReasoning?: (data: ReasoningEvent) => void;
  onCitation?: (data: Citation) => void;

  // Quota events
  onQuotaWarning?: (data: QuotaWarningEvent) => void;
  onQuotaExceeded?: (data: QuotaExceededEvent) => void;

  // OAuth consent required (external MCP tool needs user authorization)
  onOAuthRequired?: (data: OAuthRequiredEvent) => void;

  // Tool approval required (catalog flagged this MCP tool needs_approval)
  onToolApprovalRequired?: (data: ToolApprovalRequiredEvent) => void;

  // Compaction (backend rolled older turns into a summary on this turn)
  onCompaction?: (data: CompactionEvent) => void;

  // Artifact created/updated this turn (existence signal; content is
  // fetched out-of-band via a render token + sandboxed iframe)
  onArtifact?: (data: ArtifactEvent) => void;

  // MCP App UI resource for a tool result (SEP-1865). Inline event,
  // correlated to its tool-use block by toolUseId; carries the HTML to
  // render in the sandbox-proxy iframe.
  onUiResource?: (data: UiResourceEvent) => void;

  // Streamed partial tool input for a UI tool (SEP-1865
  // ui/notifications/tool-input-partial). Fires repeatedly while a UI tool's
  // arguments are still streaming, after its frame was mounted early; the
  // frame relays each healed prefix to the App for progressive rendering.
  onToolInputPartial?: (data: ToolInputPartialEvent) => void;

  // Error handling
  onError?: (data: StreamErrorEvent | ConversationalStreamErrorEvent | string) => void;
  onStreamError?: (data: ConversationalStreamErrorEvent) => void;

  // Parse errors (validation failures, JSON parse errors)
  onParseError?: (message: string) => void;
}

// =============================================================================
// Validation Functions
// =============================================================================

/**
 * Validate MessageStartEvent structure
 */
export function validateMessageStartEvent(data: unknown): data is MessageStartEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<MessageStartEvent>;
  return event.role === 'user' || event.role === 'assistant';
}

/**
 * Validate ContentBlockStartEvent structure
 *
 * NOTE: According to AWS ConverseStream API:
 * - contentBlockStart is OPTIONAL for text blocks (Claude skips it)
 * - contentBlockStart is REQUIRED for tool_use blocks
 * - Some providers emit contentBlockStart without type for text blocks
 */
export function validateContentBlockStartEvent(data: unknown): data is ContentBlockStartEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ContentBlockStartEvent>;

  // contentBlockIndex is required
  if (
    event.contentBlockIndex === undefined ||
    event.contentBlockIndex === null ||
    typeof event.contentBlockIndex !== 'number' ||
    event.contentBlockIndex < 0 ||
    !Number.isInteger(event.contentBlockIndex)
  ) {
    return false;
  }

  // Type is optional - if provided, must be valid
  if (
    event.type &&
    event.type !== 'text' &&
    event.type !== 'tool_use' &&
    event.type !== 'tool_result'
  ) {
    return false;
  }

  // Validate tool_use fields if type is tool_use
  if (event.type === 'tool_use' && event.toolUse) {
    if (!event.toolUse.toolUseId || typeof event.toolUse.toolUseId !== 'string') {
      return false;
    }
    if (!event.toolUse.name || typeof event.toolUse.name !== 'string') {
      return false;
    }
  }

  return true;
}

/**
 * Validate ContentBlockDeltaEvent structure
 *
 * NOTE: Type can be inferred from content:
 * - If 'text' field is present -> type is 'text'
 * - If 'input' field is present -> type is 'tool_use'
 */
export function validateContentBlockDeltaEvent(data: unknown): data is ContentBlockDeltaEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ContentBlockDeltaEvent>;

  // contentBlockIndex is required
  if (
    event.contentBlockIndex === undefined ||
    event.contentBlockIndex === null ||
    typeof event.contentBlockIndex !== 'number' ||
    event.contentBlockIndex < 0 ||
    !Number.isInteger(event.contentBlockIndex)
  ) {
    return false;
  }

  // Type validation if provided
  if (
    event.type &&
    event.type !== 'text' &&
    event.type !== 'tool_use' &&
    event.type !== 'tool_result'
  ) {
    return false;
  }

  // Must have at least one of: text, input
  if (event.text === undefined && event.input === undefined) {
    return false;
  }

  return true;
}

/**
 * Validate ContentBlockStopEvent structure
 */
export function validateContentBlockStopEvent(data: unknown): data is ContentBlockStopEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ContentBlockStopEvent>;

  return (
    event.contentBlockIndex !== undefined &&
    event.contentBlockIndex !== null &&
    typeof event.contentBlockIndex === 'number' &&
    event.contentBlockIndex >= 0 &&
    Number.isInteger(event.contentBlockIndex)
  );
}

/**
 * Validate MessageStopEvent structure
 */
export function validateMessageStopEvent(data: unknown): data is MessageStopEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<MessageStopEvent>;
  return typeof event.stopReason === 'string' && event.stopReason.length > 0;
}

/**
 * Validate ToolUseEvent structure
 */
export function validateToolUseEvent(data: unknown): data is ToolUseEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ToolUseEvent>;

  if (!event.tool_use || typeof event.tool_use !== 'object') {
    return false;
  }

  return (
    typeof event.tool_use.name === 'string' &&
    event.tool_use.name.length > 0 &&
    typeof event.tool_use.tool_use_id === 'string' &&
    event.tool_use.tool_use_id.length > 0
  );
}

/**
 * Validate ToolResultEventData structure
 */
export function validateToolResultEvent(data: unknown): data is ToolResultEventData {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as { tool_result?: unknown };

  if (!event.tool_result || typeof event.tool_result !== 'object') {
    return false;
  }

  const toolResult = event.tool_result as { toolUseId?: unknown };
  return typeof toolResult.toolUseId === 'string' && toolResult.toolUseId.length > 0;
}

/**
 * Validate QuotaWarningEvent structure
 */
export function validateQuotaWarningEvent(data: unknown): data is QuotaWarningEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<QuotaWarningEvent>;

  return (
    event.type === 'quota_warning' &&
    typeof event.currentUsage === 'number' &&
    typeof event.quotaLimit === 'number' &&
    typeof event.percentageUsed === 'number'
  );
}

/**
 * Validate QuotaExceededEvent structure
 */
export function validateQuotaExceededEvent(data: unknown): data is QuotaExceededEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<QuotaExceededEvent>;

  return (
    event.type === 'quota_exceeded' &&
    typeof event.currentUsage === 'number' &&
    typeof event.quotaLimit === 'number' &&
    typeof event.percentageUsed === 'number'
  );
}

/**
 * Validate ConversationalStreamErrorEvent structure
 */
export function validateConversationalStreamError(
  data: unknown,
): data is ConversationalStreamErrorEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ConversationalStreamErrorEvent>;

  return (
    event.type === 'stream_error' &&
    typeof event.code === 'string' &&
    typeof event.message === 'string' &&
    typeof event.recoverable === 'boolean'
  );
}

/**
 * Validate OAuthRequiredEvent structure
 */
export function validateOAuthRequiredEvent(data: unknown): data is OAuthRequiredEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<OAuthRequiredEvent>;

  return (
    event.type === 'oauth_required' &&
    typeof event.providerId === 'string' &&
    event.providerId.length > 0 &&
    typeof event.authorizationUrl === 'string' &&
    event.authorizationUrl.length > 0 &&
    typeof event.interruptId === 'string' &&
    event.interruptId.length > 0
  );
}

/**
 * Validate ToolApprovalRequiredEvent structure
 */
export function validateToolApprovalRequiredEvent(
  data: unknown,
): data is ToolApprovalRequiredEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ToolApprovalRequiredEvent>;

  return (
    event.type === 'tool_approval_required' &&
    typeof event.interruptId === 'string' &&
    event.interruptId.length > 0 &&
    typeof event.toolName === 'string' &&
    event.toolName.length > 0
  );
}

/**
 * Validate CompactionEvent structure
 */
export function validateCompactionEvent(data: unknown): data is CompactionEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<CompactionEvent>;

  return (
    event.type === 'compaction' &&
    typeof event.previousCheckpoint === 'number' &&
    typeof event.newCheckpoint === 'number' &&
    typeof event.summarizedTurns === 'number' &&
    event.summarizedTurns >= 0 &&
    typeof event.inputTokens === 'number' &&
    event.newCheckpoint > event.previousCheckpoint
  );
}

/**
 * Validate ArtifactEvent structure
 */
export function validateArtifactEvent(data: unknown): data is ArtifactEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ArtifactEvent>;

  return (
    event.type === 'artifact' &&
    typeof event.artifactId === 'string' &&
    event.artifactId.length > 0 &&
    typeof event.version === 'number' &&
    Number.isInteger(event.version) &&
    event.version >= 1 &&
    typeof event.title === 'string' &&
    typeof event.contentType === 'string' &&
    typeof event.sessionId === 'string' &&
    typeof event.updatedAt === 'string' &&
    (event.action === 'created' || event.action === 'updated')
  );
}

/**
 * Validate UiResourceEvent structure (SEP-1865 MCP App, PR #3 wire shape).
 *
 * `html` may legitimately be empty only in degenerate cases; we require it
 * to be a string but not non-empty so a future server that streams an
 * empty shell still round-trips. `csp`/`permissions` are objects;
 * `sandboxOrigin` may be '' until the sandbox stack is deployed.
 */
export function validateUiResourceEvent(data: unknown): data is UiResourceEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<UiResourceEvent>;

  return (
    event.type === 'ui_resource' &&
    typeof event.toolUseId === 'string' &&
    event.toolUseId.length > 0 &&
    typeof event.resourceUri === 'string' &&
    event.resourceUri.length > 0 &&
    typeof event.html === 'string' &&
    typeof event.mimeType === 'string' &&
    typeof event.sandboxOrigin === 'string' &&
    typeof event.csp === 'object' &&
    event.csp !== null &&
    typeof event.permissions === 'object' &&
    event.permissions !== null
  );
}

/**
 * Validate a tool-input-partial event (SEP-1865). `arguments` is the
 * server-healed streamed prefix of the tool input — always an object.
 */
export function validateToolInputPartialEvent(
  data: unknown,
): data is ToolInputPartialEvent {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const event = data as Partial<ToolInputPartialEvent>;

  return (
    event.type === 'ui_tool_input_partial' &&
    typeof event.toolUseId === 'string' &&
    event.toolUseId.length > 0 &&
    typeof event.arguments === 'object' &&
    event.arguments !== null &&
    !Array.isArray(event.arguments)
  );
}

/**
 * Validate Citation structure
 */
export function validateCitation(data: unknown): data is Citation {
  if (!data || typeof data !== 'object') {
    return false;
  }

  const citation = data as Partial<Citation>;

  return (
    typeof citation.assistantId === 'string' &&
    typeof citation.documentId === 'string' &&
    typeof citation.fileName === 'string' &&
    typeof citation.text === 'string'
  );
}

// =============================================================================
// Event Processing
// =============================================================================

/**
 * Process a single stream event and invoke the appropriate callback.
 *
 * This is the main entry point for handling pre-parsed events (e.g., from
 * fetch-event-source's onmessage callback).
 *
 * @param eventType - The SSE event type
 * @param data - The parsed event data
 * @param callbacks - Callbacks to invoke for each event type
 */
export function processStreamEvent(
  eventType: string,
  data: unknown,
  callbacks: StreamParserCallbacks,
): void {
  if (!eventType || typeof eventType !== 'string') {
    callbacks.onParseError?.('Invalid event type: must be a non-empty string');
    return;
  }

  try {
    switch (eventType) {
      case 'message_start':
        if (validateMessageStartEvent(data)) {
          callbacks.onMessageStart?.(data);
        } else {
          callbacks.onParseError?.('message_start: invalid data structure');
        }
        break;

      case 'content_block_start':
        if (validateContentBlockStartEvent(data)) {
          callbacks.onContentBlockStart?.(data);

          // Emit tool progress for tool_use blocks
          if (data.type === 'tool_use' && data.toolUse) {
            callbacks.onToolProgress?.({
              visible: true,
              toolName: data.toolUse.name,
              toolUseId: data.toolUse.toolUseId,
              message: `Running ${data.toolUse.name}...`,
              startTime: Date.now(),
            });
          }
        } else {
          callbacks.onParseError?.('content_block_start: invalid data structure');
        }
        break;

      case 'content_block_delta':
        if (validateContentBlockDeltaEvent(data)) {
          callbacks.onContentBlockDelta?.(data);
        } else {
          callbacks.onParseError?.('content_block_delta: invalid data structure');
        }
        break;

      case 'content_block_stop':
        if (validateContentBlockStopEvent(data)) {
          callbacks.onContentBlockStop?.(data);
        } else {
          callbacks.onParseError?.('content_block_stop: invalid data structure');
        }
        break;

      case 'tool_use':
        if (validateToolUseEvent(data)) {
          callbacks.onToolUse?.(data);
          callbacks.onToolProgress?.({
            visible: true,
            toolName: data.tool_use.name,
            toolUseId: data.tool_use.tool_use_id,
          });
        } else {
          callbacks.onParseError?.('tool_use: invalid data structure');
        }
        break;

      case 'tool_result':
        if (validateToolResultEvent(data)) {
          callbacks.onToolResult?.(data);
          callbacks.onToolProgress?.({ visible: false });
        } else {
          callbacks.onParseError?.('tool_result: invalid data structure');
        }
        break;

      case 'message_stop':
        if (validateMessageStopEvent(data)) {
          callbacks.onMessageStop?.(data);
        } else {
          callbacks.onParseError?.('message_stop: invalid data structure');
        }
        break;

      case 'done':
        callbacks.onDone?.();
        callbacks.onToolProgress?.({ visible: false });
        break;

      case 'error':
        callbacks.onError?.(data as StreamErrorEvent | string);
        break;

      case 'metadata':
        if (data && typeof data === 'object') {
          callbacks.onMetadata?.(data as MetadataEvent);
        }
        break;

      case 'reasoning':
        if (data && typeof data === 'object') {
          const reasoningData = data as ReasoningEvent;
          if (reasoningData.reasoningText) {
            callbacks.onReasoning?.(reasoningData);
          }
        }
        break;

      case 'quota_warning':
        if (validateQuotaWarningEvent(data)) {
          callbacks.onQuotaWarning?.(data);
        }
        break;

      case 'quota_exceeded':
        if (validateQuotaExceededEvent(data)) {
          callbacks.onQuotaExceeded?.(data);
        }
        break;

      case 'stream_error':
        if (validateConversationalStreamError(data)) {
          callbacks.onStreamError?.(data);
        }
        break;

      case 'citation':
        if (validateCitation(data)) {
          callbacks.onCitation?.(data);
        }
        break;

      case 'oauth_required':
        if (validateOAuthRequiredEvent(data)) {
          callbacks.onOAuthRequired?.(data);
        } else {
          callbacks.onParseError?.('oauth_required: invalid data structure');
        }
        break;

      case 'tool_approval_required':
        if (validateToolApprovalRequiredEvent(data)) {
          callbacks.onToolApprovalRequired?.(data);
        } else {
          callbacks.onParseError?.('tool_approval_required: invalid data structure');
        }
        break;

      case 'compaction':
        if (validateCompactionEvent(data)) {
          callbacks.onCompaction?.(data);
        } else {
          callbacks.onParseError?.('compaction: invalid data structure');
        }
        break;

      case 'artifact':
        if (validateArtifactEvent(data)) {
          callbacks.onArtifact?.(data);
        } else {
          callbacks.onParseError?.('artifact: invalid data structure');
        }
        break;

      case 'ui_resource':
        if (validateUiResourceEvent(data)) {
          callbacks.onUiResource?.(data);
        } else {
          callbacks.onParseError?.('ui_resource: invalid data structure');
        }
        break;

      case 'ui_tool_input_partial':
        if (validateToolInputPartialEvent(data)) {
          callbacks.onToolInputPartial?.(data);
        } else {
          callbacks.onParseError?.(
            'ui_tool_input_partial: invalid data structure',
          );
        }
        break;

      default:
        // Ignore unknown events (ping, etc.)
        break;
    }
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : 'Unknown error processing event';
    callbacks.onParseError?.(`Error processing ${eventType} event: ${errorMessage}`);
  }
}

// =============================================================================
// SSE Line Parser
// =============================================================================

/**
 * State for parsing raw SSE lines
 */
interface LineParserState {
  currentEventType: string;
}

/**
 * Create a stateful line parser for raw SSE lines.
 *
 * Use this when you're receiving raw SSE text lines (e.g., from a ReadableStream)
 * rather than pre-parsed EventSourceMessage objects.
 *
 * @param callbacks - Callbacks to invoke for each parsed event
 * @returns Object with parseLine method and reset method
 */
export function createStreamLineParser(callbacks: StreamParserCallbacks): {
  parseLine: (line: string) => void;
  reset: () => void;
} {
  const state: LineParserState = {
    currentEventType: '',
  };

  return {
    parseLine(line: string): void {
      if (!line || typeof line !== 'string') {
        callbacks.onParseError?.('parseLine: line must be a non-empty string');
        return;
      }

      // Skip empty lines and comments
      if (line.trim() === '' || line.startsWith(':')) {
        return;
      }

      // Parse event type
      if (line.startsWith('event:')) {
        const eventType = line.slice(6).trim();
        if (!eventType) {
          callbacks.onParseError?.('parseLine: event type cannot be empty');
          return;
        }
        state.currentEventType = eventType;
        return;
      }

      // Parse data
      if (line.startsWith('data:')) {
        const dataStr = line.slice(5).trim();

        // Skip empty data
        if (dataStr === '{}' || !dataStr) {
          return;
        }

        // Validate that we have an event type
        if (!state.currentEventType) {
          callbacks.onParseError?.('parseLine: received data without preceding event type');
          return;
        }

        try {
          const data = JSON.parse(dataStr);
          processStreamEvent(state.currentEventType, data, callbacks);
        } catch (e) {
          const errorMessage = e instanceof Error ? e.message : 'Unknown parsing error';
          callbacks.onParseError?.(
            `Failed to parse SSE data: ${errorMessage}. Data: ${dataStr.substring(0, 100)}`,
          );
        }
      }
    },

    reset(): void {
      state.currentEventType = '';
    },
  };
}

// =============================================================================
// Helper Utilities
// =============================================================================

/**
 * Infer content block type from delta event content
 */
export function inferContentBlockType(
  event: ContentBlockDeltaEvent,
): 'text' | 'tool_use' {
  if (event.type === 'tool_use') {
    return 'tool_use';
  }
  if (event.input !== undefined) {
    return 'tool_use';
  }
  return 'text';
}

/**
 * Best-effort extraction of a single string field's value from an incomplete
 * JSON object that is still streaming in (e.g. a tool call's `input` arriving
 * as `input_json_delta` chunks).
 *
 * Returns the decoded string value so far — even when the closing quote has
 * not yet arrived — so callers can render live "generating" feedback. Returns
 * `null` if the field's string value has not started streaming yet.
 *
 * Never throws: malformed / truncated input yields the portion decoded so far.
 * Only string-valued fields are handled (non-string values return `null`).
 *
 * @param partialJson Accumulated (possibly incomplete) JSON text
 * @param field       Top-level field name to extract (e.g. "content")
 */
export function extractStreamingStringField(
  partialJson: string,
  field: string,
): string | null {
  if (!partialJson) {
    return null;
  }

  // Locate `"field" : "` allowing JSON-permitted whitespace. Escaping the
  // field name keeps this safe even though callers pass simple identifiers.
  const escapedField = field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const opener = new RegExp(`"${escapedField}"\\s*:\\s*"`);
  const match = opener.exec(partialJson);
  if (!match) {
    return null;
  }

  let i = match.index + match[0].length;
  let result = '';

  while (i < partialJson.length) {
    const ch = partialJson[i];

    if (ch === '"') {
      // Unescaped closing quote -> value is complete.
      return result;
    }

    if (ch === '\\') {
      // Need at least one more char to know the escape; if it hasn't
      // streamed yet, drop the dangling backslash and return what we have.
      if (i + 1 >= partialJson.length) {
        return result;
      }
      const esc = partialJson[i + 1];
      switch (esc) {
        case '"':
          result += '"';
          i += 2;
          break;
        case '\\':
          result += '\\';
          i += 2;
          break;
        case '/':
          result += '/';
          i += 2;
          break;
        case 'b':
          result += '\b';
          i += 2;
          break;
        case 'f':
          result += '\f';
          i += 2;
          break;
        case 'n':
          result += '\n';
          i += 2;
          break;
        case 'r':
          result += '\r';
          i += 2;
          break;
        case 't':
          result += '\t';
          i += 2;
          break;
        case 'u': {
          // Need 4 hex digits; if the buffer cuts off mid-sequence, drop
          // the incomplete escape and return the decoded prefix.
          if (i + 6 > partialJson.length) {
            return result;
          }
          const hex = partialJson.slice(i + 2, i + 6);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
            return result;
          }
          result += String.fromCharCode(parseInt(hex, 16));
          i += 6;
          break;
        }
        default:
          // Invalid escape — emit the char literally and move on.
          result += esc;
          i += 2;
          break;
      }
      continue;
    }

    result += ch;
    i += 1;
  }

  // Buffer exhausted before the closing quote — value still streaming.
  return result;
}

/**
 * Parse tool result content array into normalized format
 */
export function parseToolResultContent(
  content: unknown[],
): Array<{ text?: string; json?: unknown; image?: { format: string; data: string } }> {
  const result: Array<{
    text?: string;
    json?: unknown;
    image?: { format: string; data: string };
  }> = [];

  for (const item of content) {
    if (!item || typeof item !== 'object') {
      continue;
    }

    const itemObj = item as Record<string, unknown>;

    // Handle text content
    if ('text' in itemObj && itemObj['text']) {
      // Try to parse as JSON first
      try {
        const parsed = JSON.parse(itemObj['text'] as string);
        result.push({ json: parsed });
      } catch {
        // Not JSON, treat as text
        result.push({ text: itemObj['text'] as string });
      }
    }

    // Handle image content
    if ('image' in itemObj && itemObj['image']) {
      const image = itemObj['image'] as Record<string, unknown>;
      let imageData: string | undefined;

      // Check for source.data or source.bytes pattern
      if (image['source'] && typeof image['source'] === 'object') {
        const source = image['source'] as Record<string, unknown>;
        imageData = (source['data'] || source['bytes']) as string | undefined;
      }
      // Check for direct data pattern
      if (!imageData && image['data']) {
        imageData = image['data'] as string;
      }

      if (imageData) {
        result.push({
          image: {
            format: (image['format'] as string) || 'png',
            data: imageData,
          },
        });
      }
    }

    // Handle JSON content directly
    if ('json' in itemObj && itemObj['json']) {
      result.push({ json: itemObj['json'] });
    }
  }

  return result;
}
