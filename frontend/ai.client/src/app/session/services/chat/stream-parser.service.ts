// services/stream-parser.service.ts
import { Injectable, signal, computed, inject } from '@angular/core';
import {
  Message,
  ContentBlock,
  MessageStartEvent,
  ContentBlockStartEvent,
  ContentBlockDeltaEvent,
  ContentBlockStopEvent,
  MessageStopEvent,
  ToolUseEvent,
  Citation,
} from '../models/message.model';
import { MetadataEvent } from '../models/content-types';
import { ChatStateService } from './chat-state.service';
import { v4 as uuidv4 } from 'uuid';
import {
  ErrorService,
  StreamErrorEvent,
  ConversationalStreamError,
} from '../../../services/error/error.service';
import {
  QuotaWarningService,
  QuotaWarning,
  QuotaExceeded,
} from '../../../services/quota/quota-warning.service';

/**
 * Internal representation of a message being built from stream events.
 * Uses a Map for O(1) content block lookups during streaming.
 */
interface MessageBuilder {
  id: string;
  role: 'user' | 'assistant';
  contentBlocks: Map<number, ContentBlockBuilder>;
  created_at: string;
  isComplete: boolean;
}

interface ContentBlockBuilder {
  index: number;
  type: 'text' | 'toolUse' | 'tool_use' | 'reasoningContent'; // Support both formats for compatibility
  // For text blocks
  textChunks: string[];
  // For tool_use blocks
  toolUseId?: string;
  toolName?: string;
  inputChunks: string[];
  // For reasoning content blocks
  reasoningChunks: string[];
  // Tool result (merged into tool_use block)
  result?: {
    content: Array<{
      text?: string;
      json?: unknown;
      image?: { format: string; data: string };
      document?: Record<string, unknown>;
    }>;
    status: 'success' | 'error';
  };
  status?: 'pending' | 'complete' | 'error';
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

/**
 * Stream state tracking
 */
enum StreamState {
  Idle = 'idle',
  Streaming = 'streaming',
  Completed = 'completed',
  Error = 'error',
}

@Injectable({
  providedIn: 'root',
})
export class StreamParserService {
  private chatStateService = inject(ChatStateService);
  private errorService = inject(ErrorService);
  private quotaWarningService = inject(QuotaWarningService);

  // =========================================================================
  // State Signals
  // =========================================================================

  /** The current message being streamed */
  private currentMessageBuilder = signal<MessageBuilder | null>(null);

  /** Completed messages in the current turn (for multi-turn tool use) */
  private completedMessages = signal<Message[]>([]);

  /** Tool progress indicator state */
  private toolProgressSignal = signal<ToolProgress>({ visible: false });
  public toolProgress = this.toolProgressSignal.asReadonly();

  /** Error state */
  private errorSignal = signal<string | null>(null);
  public error = this.errorSignal.asReadonly();

  /** Stream completion state */
  private isStreamCompleteSignal = signal<boolean>(false);
  public isStreamComplete = this.isStreamCompleteSignal.asReadonly();

  /** Metadata (usage, metrics) from the stream */
  private metadataSignal = signal<MetadataEvent | null>(null);

  /** Pending citations for the next assistant message */
  private pendingCitations = signal<Citation[]>([]);
  public citations = this.pendingCitations.asReadonly();
  public metadata = this.metadataSignal.asReadonly();

  // =========================================================================
  // Message ID Computation State
  // =========================================================================

  /** Session ID for computing message IDs */
  private sessionId: string | null = null;

  /** Starting message count for ID computation */
  private startingMessageCount: number = 0;

  // =========================================================================
  // Computed Signals - Reactive Derived State
  // =========================================================================

  /**
   * The current message converted to the final Message format.
   * Efficiently rebuilds only when the builder changes.
   */
  public currentMessage = computed<Message | null>(() => {
    const builder = this.currentMessageBuilder();
    return builder ? this.buildMessage(builder) : null;
  });

  /**
   * All messages in the current streaming session (completed + current).
   * This is what the UI should bind to for rendering.
   */
  public allMessages = computed<Message[]>(() => {
    const completed = this.completedMessages();
    const current = this.currentMessage();
    return current ? [...completed, current] : completed;
  });

  /**
   * The latest message's text content as a single string.
   * Useful for simple text displays.
   */
  public currentText = computed<string>(() => {
    const message = this.currentMessage();
    if (!message) return '';

    return message.content
      .filter((block) => block.type === 'text' && block.text)
      .map((block) => block.text!)
      .join('');
  });

  /**
   * Whether we're currently in the middle of a tool use cycle.
   */
  public isToolUseInProgress = computed<boolean>(() => {
    const builder = this.currentMessageBuilder();
    if (!builder) return false;

    return Array.from(builder.contentBlocks.values()).some(
      (block) => (block.type === 'toolUse' || block.type === 'tool_use') && !block.isComplete,
    );
  });

  /**
   * The ID of the message currently being streamed, or null if not streaming.
   * Used by UI components to determine which message should animate.
   */
  public streamingMessageId = computed<string | null>(() => {
    const builder = this.currentMessageBuilder();
    const isComplete = this.isStreamCompleteSignal();

    // Return the message ID if we have an active builder and stream is not complete
    if (builder && !isComplete) {
      return builder.id;
    }
    return null;
  });

  // =========================================================================
  // Public API
  // =========================================================================

  /**
   * Parse an incoming SSE line and update state.
   * Handles the event: and data: format from SSE.
   */
  parseSSELine(line: string): void {
    // Validate input
    if (!line || typeof line !== 'string') {
      this.setError('parseSSELine: line must be a non-empty string');
      return;
    }

    // Skip empty lines and comments
    if (line.trim() === '' || line.startsWith(':')) return;

    // Parse event type and data
    if (line.startsWith('event:')) {
      const eventType = line.slice(6).trim();
      if (!eventType) {
        this.setError('parseSSELine: event type cannot be empty');
        return;
      }
      this.currentEventType = eventType;
      return;
    }

    if (line.startsWith('data:')) {
      const dataStr = line.slice(5).trim();

      // Skip empty data
      if (dataStr === '{}' || !dataStr) return;

      // Validate that we have an event type set
      if (!this.currentEventType) {
        this.setError('parseSSELine: received data without preceding event type');
        return;
      }

      try {
        const data = JSON.parse(dataStr);
        this.handleEvent(this.currentEventType, data);
      } catch (e) {
        const errorMessage = e instanceof Error ? e.message : 'Unknown parsing error';
        this.setError(
          `Failed to parse SSE data: ${errorMessage}. Data: ${dataStr.substring(0, 100)}`,
        );
      }
    }
  }

  /**
   * Parse a pre-parsed EventSourceMessage (from fetch-event-source).
   */
  parseEventSourceMessage(event: string, data: unknown): void {
    // Validate inputs
    if (!event || typeof event !== 'string') {
      this.setError('parseEventSourceMessage: event must be a non-empty string');
      return;
    }

    if (data === undefined || data === null) {
      // Some events may have null/undefined data (like 'done')
      if (event === 'done') {
        this.handleEvent(event, data);
        return;
      }
      this.setError(`parseEventSourceMessage: data cannot be null/undefined for event '${event}'`);
      return;
    }

    this.handleEvent(event, data);
  }

  /**
   * Reset all state for a new conversation/stream.
   * Generates a new stream ID to prevent race conditions.
   *
   * IMPORTANT: Call this before starting a new stream to prevent
   * events from previous streams from interfering.
   *
   * @param sessionId - Session ID for computing predictable message IDs
   * @param startingMessageCount - Current message count in the session (for ID computation)
   */
  reset(sessionId?: string, startingMessageCount?: number): void {
    // Generate new stream ID to prevent events from old streams
    // This invalidates any in-flight events from previous streams
    const oldStreamId = this.currentStreamId;
    this.currentStreamId = uuidv4();
    this.streamState = StreamState.Idle;

    // Store session ID and message count for predictable ID generation
    this.sessionId = sessionId || null;
    this.startingMessageCount = startingMessageCount || 0;

    // Clear all state
    this.currentMessageBuilder.set(null);
    this.completedMessages.set([]);
    this.toolProgressSignal.set({ visible: false });
    this.errorSignal.set(null);
    this.isStreamCompleteSignal.set(false);
    this.metadataSignal.set(null);
    this.pendingCitations.set([]);
    this.currentEventType = '';
  }

  /**
   * Get the current stream ID (for debugging/monitoring).
   */
  getCurrentStreamId(): string | null {
    return this.currentStreamId;
  }

  // =========================================================================
  // Validation Helpers
  // =========================================================================

  /**
   * Validate that a content block index is valid.
   */
  private validateContentBlockIndex(index: number | undefined, eventType: string): boolean {
    if (index === undefined || index === null) {
      this.setError(`${eventType}: contentBlockIndex is required`);
      return false;
    }

    if (typeof index !== 'number' || index < 0 || !Number.isInteger(index)) {
      this.setError(`${eventType}: contentBlockIndex must be a non-negative integer, got ${index}`);
      return false;
    }

    return true;
  }

  /**
   * Validate MessageStartEvent data structure.
   */
  private validateMessageStartEvent(data: unknown): data is MessageStartEvent {
    if (!data || typeof data !== 'object') {
      this.setError('message_start: data must be an object');
      return false;
    }

    const event = data as Partial<MessageStartEvent>;
    if (!event.role || (event.role !== 'user' && event.role !== 'assistant')) {
      this.setError(`message_start: role must be 'user' or 'assistant', got ${event.role}`);
      return false;
    }

    return true;
  }

  /**
   * Validate ContentBlockStartEvent data structure.
   *
   * NOTE: According to AWS ConverseStream API docs, contentBlockStart is:
   * - OPTIONAL for text blocks (Claude skips it entirely for text)
   * - REQUIRED for tool_use blocks (contains toolUseId and name)
   *
   * Some providers (like Gemini via Strands) emit contentBlockStart without
   * a type field for text blocks. We accept this and default to 'text'.
   */
  private validateContentBlockStartEvent(data: unknown): data is ContentBlockStartEvent {
    if (!data || typeof data !== 'object') {
      this.setError('content_block_start: data must be an object');
      return false;
    }

    const event = data as Partial<ContentBlockStartEvent>;

    if (!this.validateContentBlockIndex(event.contentBlockIndex, 'content_block_start')) {
      return false;
    }

    // Type is optional - if missing, we'll default to 'text' in the handler
    // This handles providers that emit contentBlockStart without type for text blocks
    if (
      event.type &&
      event.type !== 'text' &&
      event.type !== 'tool_use' &&
      event.type !== 'tool_result'
    ) {
      this.setError(
        `content_block_start: type must be 'text', 'tool_use', or 'tool_result', got ${event.type}`,
      );
      return false;
    }

    // Validate tool_use specific fields only when type is explicitly tool_use
    if (event.type === 'tool_use' && event.toolUse) {
      if (!event.toolUse.toolUseId || typeof event.toolUse.toolUseId !== 'string') {
        this.setError('content_block_start: toolUse.toolUseId must be a non-empty string');
        return false;
      }
      if (!event.toolUse.name || typeof event.toolUse.name !== 'string') {
        this.setError('content_block_start: toolUse.name must be a non-empty string');
        return false;
      }
    }

    return true;
  }

  /**
   * Validate ContentBlockDeltaEvent data structure.
   *
   * NOTE: The 'type' field may be inferred from content:
   * - If 'text' field is present -> type is 'text'
   * - If 'input' field is present -> type is 'tool_use'
   * This handles providers that may not always include explicit type.
   */
  private validateContentBlockDeltaEvent(data: unknown): data is ContentBlockDeltaEvent {
    if (!data || typeof data !== 'object') {
      this.setError('content_block_delta: data must be an object');
      return false;
    }

    const event = data as Partial<ContentBlockDeltaEvent>;

    if (!this.validateContentBlockIndex(event.contentBlockIndex, 'content_block_delta')) {
      return false;
    }

    // Type can be explicit or inferred from content
    // If type is provided, validate it's a known type
    if (
      event.type &&
      event.type !== 'text' &&
      event.type !== 'tool_use' &&
      event.type !== 'tool_result'
    ) {
      this.setError(
        `content_block_delta: type must be 'text', 'tool_use', or 'tool_result', got ${event.type}`,
      );
      return false;
    }

    // Must have at least one of: text, input (for type inference and content)
    // If type is missing, we can infer from content in the handler
    if (event.text === undefined && event.input === undefined) {
      this.setError('content_block_delta: must have either text or input field');
      return false;
    }

    return true;
  }

  /**
   * Validate ContentBlockStopEvent data structure.
   */
  private validateContentBlockStopEvent(data: unknown): data is ContentBlockStopEvent {
    if (!data || typeof data !== 'object') {
      this.setError('content_block_stop: data must be an object');
      return false;
    }

    const event = data as Partial<ContentBlockStopEvent>;

    if (!this.validateContentBlockIndex(event.contentBlockIndex, 'content_block_stop')) {
      return false;
    }

    return true;
  }

  /**
   * Validate MessageStopEvent data structure.
   */
  private validateMessageStopEvent(data: unknown): data is MessageStopEvent {
    if (!data || typeof data !== 'object') {
      this.setError('message_stop: data must be an object');
      return false;
    }

    const event = data as Partial<MessageStopEvent>;

    if (!event.stopReason || typeof event.stopReason !== 'string') {
      this.setError('message_stop: stopReason must be a non-empty string');
      return false;
    }

    return true;
  }

  /**
   * Validate ToolUseEvent data structure.
   */
  private validateToolUseEvent(data: unknown): data is ToolUseEvent {
    if (!data || typeof data !== 'object') {
      this.setError('tool_use: data must be an object');
      return false;
    }

    const event = data as Partial<ToolUseEvent>;

    if (!event.tool_use || typeof event.tool_use !== 'object') {
      this.setError('tool_use: tool_use field must be an object');
      return false;
    }

    if (!event.tool_use.name || typeof event.tool_use.name !== 'string') {
      this.setError('tool_use: tool_use.name must be a non-empty string');
      return false;
    }

    if (!event.tool_use.tool_use_id || typeof event.tool_use.tool_use_id !== 'string') {
      this.setError('tool_use: tool_use.tool_use_id must be a non-empty string');
      return false;
    }

    return true;
  }

  /**
   * Get completed messages and clear them (for persisting to backend).
   */
  flushCompletedMessages(): Message[] {
    const messages = this.completedMessages();
    this.completedMessages.set([]);
    return messages;
  }

  /**
   * Check if an event should be processed based on stream ID.
   * Prevents race conditions from overlapping streams.
   */
  private shouldProcessEvent(): boolean {
    // If no stream ID is set, we're not in a valid streaming state
    if (!this.currentStreamId) {
      // Allow first event to set up stream (message_start)
      return true;
    }

    // If stream is completed or errored, reject new events
    if (this.streamState === StreamState.Completed || this.streamState === StreamState.Error) {
      return false;
    }

    return true;
  }

  // =========================================================================
  // Private State
  // =========================================================================

  private currentEventType = '';

  /** Current stream ID - prevents race conditions from overlapping streams */
  private currentStreamId: string | null = null;

  /** Current stream state */
  private streamState: StreamState = StreamState.Idle;

  // =========================================================================
  // Event Routing
  // =========================================================================

  private handleEvent(eventType: string, data: unknown): void {
    // Validate event type
    if (!eventType || typeof eventType !== 'string') {
      this.setError('Invalid event type: must be a non-empty string');
      return;
    }

    // Check if we should process this event (prevents race conditions)
    // Allow message_start and error events even if stream appears complete
    const isStartOrErrorEvent = eventType === 'message_start' || eventType === 'error';
    if (!isStartOrErrorEvent && !this.shouldProcessEvent()) {
      return;
    }

    try {
      switch (eventType) {
        case 'message_start':
          this.handleMessageStart(data);
          break;

        case 'content_block_start':
          this.handleContentBlockStart(data);
          break;

        case 'content_block_delta':
          this.handleContentBlockDelta(data);
          break;

        case 'content_block_stop':
          this.handleContentBlockStop(data);
          break;

        case 'tool_use':
          this.handleToolUseProgress(data);
          break;

        case 'tool_result':
          this.handleToolResult(data);
          break;

        case 'message_stop':
          this.handleMessageStop(data);
          break;

        case 'done':
          this.handleDone();
          break;

        case 'error':
          this.handleError(data);
          break;

        case 'metadata':
          this.handleMetadata(data);
          break;

        case 'reasoning':
          this.handleReasoning(data);
          break;

        case 'quota_warning':
          this.handleQuotaWarning(data);
          break;

        case 'quota_exceeded':
          this.handleQuotaExceeded(data);
          break;

        case 'stream_error':
          this.handleStreamErrorEvent(data);
          break;

        case 'citation':
          this.handleCitation(data);
          break;

        default:
          // Ignore unknown events (ping, etc.)
          break;
      }
    } catch (error) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error processing event';
      this.setError(`Error processing ${eventType} event: ${errorMessage}`);
    }
  }

  // =========================================================================
  // Error Handling Helpers
  // =========================================================================

  /**
   * Set error state and mark stream as complete.
   */
  private setError(message: string): void {
    this.errorSignal.set(message);
    this.isStreamCompleteSignal.set(true);
    this.toolProgressSignal.set({ visible: false });
    this.streamState = StreamState.Error;
  }

  /**
   * Clear error state.
   */
  private clearError(): void {
    this.errorSignal.set(null);
  }

  // =========================================================================
  // Event Handlers
  // =========================================================================

  private handleMessageStart(data: unknown): void {
    // Validate event data
    if (!this.validateMessageStartEvent(data)) {
      return; // Error already set by validator
    }

    const eventData = data as MessageStartEvent;

    // Initialize stream ID if not set (handles case where reset() wasn't called)
    if (!this.currentStreamId) {
      this.currentStreamId = uuidv4();
    }

    // Update stream state
    this.streamState = StreamState.Streaming;

    // Clear any previous errors when starting a new message
    this.clearError();

    // If there's an existing message, finalize it before starting a new one
    const currentBuilder = this.currentMessageBuilder();
    if (currentBuilder) {
      this.finalizeCurrentMessage();
    }

    // Clear stopReason in ChatStateService
    this.chatStateService.setStopReason(null);

    // Compute predictable message ID: msg-{sessionId}-{index}
    // The index is: startingMessageCount + number of completed messages in this stream
    const completedCount = this.completedMessages().length;
    const messageIndex = this.startingMessageCount + completedCount;
    const computedId = this.sessionId ? `msg-${this.sessionId}-${messageIndex}` : uuidv4();

    // Create new message builder with computed ID
    const builder: MessageBuilder = {
      id: computedId,
      role: eventData.role,
      contentBlocks: new Map(),
      created_at: new Date().toISOString(),
      isComplete: false,
    };

    this.currentMessageBuilder.set(builder);
  }

  private handleContentBlockStart(data: unknown): void {
    // Validate event data
    if (!this.validateContentBlockStartEvent(data)) {
      return; // Error already set by validator
    }

    const eventData = data as ContentBlockStartEvent;

    // Ensure we have an active message builder
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(
        'content_block_start: received without active message. Ensure message_start was called first.',
      );
      return;
    }

    // Check if block already exists
    if (currentBuilder.contentBlocks.has(eventData.contentBlockIndex)) {
      this.setError(
        `content_block_start: block at index ${eventData.contentBlockIndex} already exists`,
      );
      return;
    }

    // Determine block type - default to 'text' if not specified
    // AWS ConverseStream API: contentBlockStart without type indicates text block
    // Tool use blocks will have explicit type='tool_use' with toolUse data
    const blockType: 'text' | 'tool_use' = eventData.type === 'tool_use' ? 'tool_use' : 'text';

    this.currentMessageBuilder.update((builder) => {
      if (!builder) {
        // This shouldn't happen after the check above, but handle defensively
        return builder;
      }

      const blockBuilder: ContentBlockBuilder = {
        index: eventData.contentBlockIndex,
        type: blockType,
        textChunks: [],
        inputChunks: [],
        reasoningChunks: [],
        toolUseId: eventData.toolUse?.toolUseId,
        toolName: eventData.toolUse?.name,
        isComplete: false,
      };

      // Create new Map to trigger reactivity
      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(eventData.contentBlockIndex, blockBuilder);

      return {
        ...builder,
        contentBlocks: newBlocks,
      };
    });

    // Show tool progress for tool_use blocks
    if (blockType === 'tool_use' && eventData.toolUse) {
      this.toolProgressSignal.set({
        visible: true,
        toolName: eventData.toolUse.name,
        toolUseId: eventData.toolUse.toolUseId,
        message: `Running ${eventData.toolUse.name}...`,
        startTime: Date.now(),
      });
    }
  }

  private handleContentBlockDelta(data: unknown): void {
    // Validate event data
    if (!this.validateContentBlockDeltaEvent(data)) {
      return; // Error already set by validator
    }

    const eventData = data as ContentBlockDeltaEvent;

    // Infer type from content if not provided
    // AWS ConverseStream: text field -> text block, input field -> tool_use block
    const inferredType: 'text' | 'tool_use' =
      eventData.type === 'tool_use'
        ? 'tool_use'
        : eventData.input !== undefined
          ? 'tool_use'
          : 'text';

    // Ensure we have an active message builder
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(
        'content_block_delta: received without active message. Ensure message_start was called first.',
      );
      return;
    }

    this.currentMessageBuilder.update((builder) => {
      if (!builder) {
        // This shouldn't happen after the check above, but handle defensively
        return builder;
      }

      let block = builder.contentBlocks.get(eventData.contentBlockIndex);

      // Auto-create block if it doesn't exist (Claude skips content_block_start for text)
      if (!block) {
        block = {
          index: eventData.contentBlockIndex,
          type: inferredType,
          textChunks: [],
          inputChunks: [],
          reasoningChunks: [],
          isComplete: false,
        };
      }

      // Upgrade block type if needed (text -> tool_use)
      // This handles edge cases where block was created as text but receives tool_use delta
      if (block.type === 'text' && inferredType === 'tool_use') {
        block.type = 'tool_use';
      }

      // Update the appropriate chunks based on inferred type
      if (eventData.text !== undefined) {
        if (typeof eventData.text !== 'string') {
          this.setError(
            `content_block_delta: text field must be a string, got ${typeof eventData.text}`,
          );
          return builder;
        }
        block.textChunks.push(eventData.text);
      }

      if (eventData.input !== undefined) {
        if (typeof eventData.input !== 'string') {
          this.setError(
            `content_block_delta: input field must be a string, got ${typeof eventData.input}`,
          );
          return builder;
        }
        block.inputChunks.push(eventData.input);
      }

      // Create new Map reference to trigger reactivity
      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(eventData.contentBlockIndex, { ...block });

      return {
        ...builder,
        contentBlocks: newBlocks,
      };
    });
  }

  private handleContentBlockStop(data: unknown): void {
    // Validate event data
    if (!this.validateContentBlockStopEvent(data)) {
      return; // Error already set by validator
    }

    const eventData = data as ContentBlockStopEvent;

    // Ensure we have an active message builder
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(
        'content_block_stop: received without active message. Ensure message_start was called first.',
      );
      return;
    }

    this.currentMessageBuilder.update((builder) => {
      if (!builder) {
        // This shouldn't happen after the check above, but handle defensively
        return builder;
      }

      const block = builder.contentBlocks.get(eventData.contentBlockIndex);
      if (!block) {
        this.setError(
          `content_block_stop: block at index ${eventData.contentBlockIndex} does not exist`,
        );
        return builder;
      }

      // Check if block is already complete
      if (block.isComplete) {
        // Allow duplicate stop events (idempotent)
        return builder;
      }

      block.isComplete = true;

      // Hide tool progress when tool block completes
      if (block.type === 'tool_use') {
        this.toolProgressSignal.set({ visible: false });
      }

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(eventData.contentBlockIndex, { ...block });

      return {
        ...builder,
        contentBlocks: newBlocks,
      };
    });
  }

  private handleToolUseProgress(data: unknown): void {
    // This event provides accumulated tool input - useful for progress display
    // The actual content is built from content_block_delta events

    // Validate event data
    if (!this.validateToolUseEvent(data)) {
      return; // Error already set by validator
    }

    const eventData = data as ToolUseEvent;

    if (eventData.tool_use) {
      this.toolProgressSignal.update((progress) => ({
        ...progress,
        visible: true,
        toolName: eventData.tool_use.name,
        toolUseId: eventData.tool_use.tool_use_id,
      }));
    }
  }

  private handleToolResult(data: unknown): void {
    // Validate data structure
    if (!data || typeof data !== 'object') {
      this.setError('tool_result: data must be an object');
      return;
    }

    // The backend sends: { tool_result: { toolUseId, content, status } }
    const rawData = data as { tool_result?: any };
    const toolResultData = rawData.tool_result;

    if (!toolResultData || typeof toolResultData !== 'object') {
      this.setError('tool_result: tool_result field must be an object');
      return;
    }

    const toolUseId = toolResultData.toolUseId;
    if (!toolUseId || typeof toolUseId !== 'string') {
      this.setError('tool_result: toolUseId must be a non-empty string');
      return;
    }

    const content = toolResultData.content || [];
    const status = toolResultData.status || 'success';

    // Ensure we have an active message builder
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(
        'tool_result: received without active message. Ensure message_start was called first.',
      );
      return;
    }

    // Find the tool_use block that matches this toolUseId
    let foundBlock: ContentBlockBuilder | null = null;
    let foundIndex: number | null = null;

    for (const [index, block] of currentBuilder.contentBlocks.entries()) {
      if (
        (block.type === 'tool_use' || block.type === 'toolUse') &&
        block.toolUseId === toolUseId
      ) {
        foundBlock = block;
        foundIndex = index;
        break;
      }
    }

    if (!foundBlock || foundIndex === null) {
      return;
    }

    // Merge the result into the tool_use block
    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      const block = builder.contentBlocks.get(foundIndex!);
      if (!block) return builder;

      // Build result content array from the backend's content array
      const resultContent: Array<{
        text?: string;
        json?: unknown;
        image?: { format: string; data: string };
      }> = [];

      // Process content array from backend
      if (Array.isArray(content)) {
        for (const item of content) {
          if (!item || typeof item !== 'object') continue;

          // Handle text content
          if ('text' in item && item.text) {
            // Try to parse as JSON first
            try {
              const parsed = JSON.parse(item.text);
              resultContent.push({ json: parsed });
            } catch {
              // Not JSON, treat as text
              resultContent.push({ text: item.text });
            }
          }

          // Handle image content
          // Supports both formats:
          // - { format, source: { bytes/data } } - from Code Interpreter tool result
          // - { format, data } - from ToolResultProcessor (SSE events)
          if ('image' in item && item.image) {
            const image = item.image;
            let imageData: string | undefined;

            // Check for source.data or source.bytes pattern
            if (image.source) {
              imageData = image.source.data || image.source.bytes;
            }
            // Check for direct data pattern (from ToolResultProcessor)
            if (!imageData && image.data) {
              imageData = image.data;
            }

            if (imageData) {
              resultContent.push({
                image: {
                  format: image.format || 'png',
                  data: imageData,
                },
              });
            }
          }

          // Handle JSON content directly
          if ('json' in item && item.json) {
            resultContent.push({ json: item.json });
          }
        }
      }

      // Update the block with result
      const updatedBlock: ContentBlockBuilder = {
        ...block,
        result: {
          content: resultContent,
          status: status,
        },
        status: (status === 'error' ? 'error' : 'complete') as 'pending' | 'complete' | 'error',
      };

      // Create new Map reference to trigger reactivity
      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(foundIndex!, updatedBlock);

      return {
        ...builder,
        contentBlocks: newBlocks,
      };
    });

    // Hide tool progress
    this.toolProgressSignal.set({ visible: false });
  }

  private handleMessageStop(data: unknown): void {
    // Validate event data
    if (!this.validateMessageStopEvent(data)) {
      return; // Error already set by validator
    }

    const eventData = data as MessageStopEvent;

    // Ensure we have an active message builder
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(
        'message_stop: received without active message. Ensure message_start was called first.',
      );
      return;
    }

    // Set stopReason in ChatStateService
    this.chatStateService.setStopReason(eventData.stopReason);

    // Mark message as complete - ID was already set from message_start
    this.currentMessageBuilder.update((builder) => {
      if (!builder) {
        // This shouldn't happen after the check above, but handle defensively
        return builder;
      }

      return {
        ...builder,
        isComplete: true,
      };
    });

    // If stop reason is tool_use, keep the message active for tool result
    // Otherwise, finalize it
    if (eventData.stopReason !== 'tool_use') {
      this.finalizeCurrentMessage();
    }
  }

  private handleDone(): void {
    this.finalizeCurrentMessage();
    this.isStreamCompleteSignal.set(true);
    this.toolProgressSignal.set({ visible: false });
    this.streamState = StreamState.Completed;

    // Automatic cleanup: flush completed messages after a short delay
    // This prevents memory buildup while allowing UI to read messages
    setTimeout(() => {
      // Only flush if stream is still completed (not reset)
      if (this.streamState === StreamState.Completed) {
        this.flushCompletedMessages();
      }
    }, 5000); // 5 second delay to allow UI to read messages
  }

  private handleError(data: unknown): void {
    let errorMessage = 'Unknown error';

    // Check if this is a structured error event from backend
    if (data && typeof data === 'object') {
      const potentialStructuredError = data as Partial<StreamErrorEvent>;

      if (potentialStructuredError.error && potentialStructuredError.code) {
        // This is a structured StreamErrorEvent from backend
        const streamError: StreamErrorEvent = {
          error: potentialStructuredError.error,
          code: potentialStructuredError.code,
          detail: potentialStructuredError.detail,
          recoverable: potentialStructuredError.recoverable ?? false,
          metadata: potentialStructuredError.metadata,
        };

        // Use ErrorService to display the error
        this.errorService.handleStreamError(streamError);
        errorMessage = streamError.error;
      } else {
        // Legacy unstructured error
        const errorData = data as { error?: string; message?: string };
        errorMessage = errorData.error || errorData.message || errorMessage;

        // Add to ErrorService with generic code
        this.errorService.addError('Stream Error', errorMessage);
      }
    } else if (typeof data === 'string') {
      errorMessage = data;
      this.errorService.addError('Stream Error', errorMessage);
    } else if (data instanceof Error) {
      errorMessage = data.message;
      this.errorService.addError('Stream Error', errorMessage);
    }

    this.setError(`Stream error: ${errorMessage}`);
  }

  private handleMetadata(data: unknown): void {
    if (!data || typeof data !== 'object') {
      return;
    }

    const metadataData = data as MetadataEvent;

    // Validate that at least usage or metrics is present
    if (!metadataData.usage && !metadataData.metrics) {
      return;
    }

    // Update metadata signal
    this.metadataSignal.set(metadataData);

    // Update the last completed message with metadata if it doesn't have it yet
    this.updateLastCompletedMessageWithMetadata();
  }

  /**
   * Handle reasoning events from the SSE stream.
   * These contain chain-of-thought reasoning text from models like Claude 3.7+ or GPT.
   *
   * The reasoning events are separate from content_block_delta events and need
   * to be accumulated into a reasoningContent block.
   */
  private handleReasoning(data: unknown): void {
    if (!data || typeof data !== 'object') {
      return;
    }

    const reasoningData = data as { reasoningText?: string };

    // Skip empty reasoning events (some are just markers)
    if (!reasoningData.reasoningText) {
      return;
    }

    // Ensure we have an active message builder
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      // Create a new message builder if one doesn't exist
      // This handles edge cases where reasoning arrives before message_start
      return;
    }

    this.currentMessageBuilder.update((builder) => {
      if (!builder) {
        return builder;
      }

      // Find or create the reasoning content block
      // We use a special index (-1) to track reasoning separately, then
      // insert it at the correct position when building the final message
      // Actually, looking at the SSE events, reasoning has contentBlockIndex from the backend
      // But since we're skipping content_block_delta for reasoning, we need to track it ourselves

      // Find existing reasoning block or create one
      let reasoningBlock: ContentBlockBuilder | undefined;
      let reasoningIndex: number = -1;

      for (const [index, block] of builder.contentBlocks.entries()) {
        if (block.type === 'reasoningContent') {
          reasoningBlock = block;
          reasoningIndex = index;
          break;
        }
      }

      // If no reasoning block exists, create one
      // Use the next available index
      if (!reasoningBlock) {
        const maxIndex = Math.max(-1, ...Array.from(builder.contentBlocks.keys()));
        reasoningIndex = maxIndex + 1;

        // Check if we need to insert at position 0 (reasoning typically comes first)
        // But we need to be careful not to conflict with existing blocks
        // For simplicity, we'll just use a high index and rely on sorting later
        reasoningBlock = {
          index: reasoningIndex,
          type: 'reasoningContent',
          textChunks: [],
          inputChunks: [],
          reasoningChunks: [],
          isComplete: false,
        };
      }

      // Append the reasoning text
      reasoningBlock.reasoningChunks.push(reasoningData.reasoningText!);

      // Create new Map reference to trigger reactivity
      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(reasoningIndex, { ...reasoningBlock });

      return {
        ...builder,
        contentBlocks: newBlocks,
      };
    });
  }

  /**
   * Handle quota_warning events from the SSE stream.
   * These are sent when user usage exceeds soft_limit_percentage on their tier.
   */
  private handleQuotaWarning(data: unknown): void {
    if (!data || typeof data !== 'object') {
      return;
    }

    const warningData = data as Partial<QuotaWarning>;

    // Validate required fields
    if (
      warningData.type !== 'quota_warning' ||
      typeof warningData.currentUsage !== 'number' ||
      typeof warningData.quotaLimit !== 'number' ||
      typeof warningData.percentageUsed !== 'number'
    ) {
      return;
    }

    // Delegate to QuotaWarningService
    this.quotaWarningService.setWarning(warningData as QuotaWarning);
  }

  /**
   * Handle quota_exceeded events from the SSE stream.
   * These are sent when user has exceeded their quota limit.
   * The message is already displayed as an assistant response, but this
   * event provides additional metadata for UI enhancements.
   */
  private handleQuotaExceeded(data: unknown): void {
    if (!data || typeof data !== 'object') {
      return;
    }

    const exceededData = data as Partial<QuotaExceeded>;

    // Validate required fields
    if (
      exceededData.type !== 'quota_exceeded' ||
      typeof exceededData.currentUsage !== 'number' ||
      typeof exceededData.quotaLimit !== 'number' ||
      typeof exceededData.percentageUsed !== 'number'
    ) {
      return;
    }

    // Delegate to QuotaWarningService
    this.quotaWarningService.setQuotaExceeded(exceededData as QuotaExceeded);
  }

  /**
   * Handle stream_error events from the SSE stream.
   * These are conversational errors that are already displayed as assistant messages.
   * The error message appears in the chat, so we only track state for potential retry.
   */
  private handleStreamErrorEvent(data: unknown): void {
    if (!data || typeof data !== 'object') {
      return;
    }

    const errorData = data as Partial<ConversationalStreamError>;

    // Validate required fields
    if (
      errorData.type !== 'stream_error' ||
      !errorData.code ||
      typeof errorData.message !== 'string' ||
      typeof errorData.recoverable !== 'boolean'
    ) {
      return;
    }

    // Delegate to ErrorService - it will track state without showing duplicate toast
    this.errorService.handleConversationalStreamError(errorData as ConversationalStreamError);
  }

  /**
   * Handle citation events from the SSE stream.
   * Citations arrive BEFORE message_start and are accumulated in pendingCitations.
   * When the next assistant message starts, citations are attached to it.
   */
  private handleCitation(data: unknown): void {
    if (!data || typeof data !== 'object') {
      return;
    }

    const citationData = data as Partial<Citation>;

    // Validate required fields
    if (
      typeof citationData.assistantId !== 'string' ||
      typeof citationData.documentId !== 'string' ||
      typeof citationData.fileName !== 'string' ||
      typeof citationData.text !== 'string'
    ) {
      return;
    }

    // Accumulate citation in pending citations
    this.pendingCitations.update((citations) => [
      ...citations,
      {
        assistantId: citationData.assistantId!,
        documentId: citationData.documentId!,
        fileName: citationData.fileName!,
        text: citationData.text!,
      },
    ]);
  }

  /**
   * Update the last completed message with metadata if it doesn't have it yet.
   * This handles the case where metadata arrives after a message is finalized.
   * Also updates if new metadata has more complete information (e.g., TTFT, cost).
   */
  private updateLastCompletedMessageWithMetadata(): void {
    const completed = this.completedMessages();
    if (completed.length === 0) return;

    const lastMessage = completed[completed.length - 1];
    const newMetadata = this.getMetadataForMessage();
    if (!newMetadata) return;

    // Always update if message doesn't have metadata
    if (!lastMessage.metadata) {
      this.completedMessages.update((messages) => {
        const updated = [...messages];
        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          metadata: newMetadata,
        };
        return updated;
      });
      return;
    }

    // Check if we need to update - compare existing vs new metadata
    const existingMetadata = lastMessage.metadata as Record<string, unknown>;
    const existingLatency = existingMetadata['latency'] as
      | { timeToFirstToken?: number }
      | undefined;
    const existingTTFT = existingLatency?.timeToFirstToken;
    const existingCost = existingMetadata['cost'] as number | undefined;
    const existingTokenUsage = existingMetadata['tokenUsage'] as
      | {
          cacheReadInputTokens?: number;
          cacheWriteInputTokens?: number;
        }
      | undefined;

    const newLatency = newMetadata['latency'] as { timeToFirstToken?: number } | undefined;
    const newTTFT = newLatency?.timeToFirstToken;
    const newCost = newMetadata['cost'] as number | undefined;
    const newTokenUsage = newMetadata['tokenUsage'] as
      | {
          cacheReadInputTokens?: number;
          cacheWriteInputTokens?: number;
        }
      | undefined;

    // Update if new metadata has fields that existing doesn't have
    const needsTTFTUpdate = !existingTTFT && newTTFT;
    const needsCostUpdate = existingCost === undefined && newCost !== undefined;
    const needsCacheUpdate =
      (existingTokenUsage?.cacheReadInputTokens === undefined &&
        newTokenUsage?.cacheReadInputTokens !== undefined) ||
      (existingTokenUsage?.cacheWriteInputTokens === undefined &&
        newTokenUsage?.cacheWriteInputTokens !== undefined);

    if (needsTTFTUpdate || needsCostUpdate || needsCacheUpdate) {
      // Merge new metadata with existing (prefer new values)
      this.completedMessages.update((messages) => {
        const updated = [...messages];
        const existingLatencyObj = existingMetadata['latency'] as
          | Record<string, unknown>
          | undefined;
        const newLatencyObj = newMetadata['latency'] as Record<string, unknown> | undefined;
        const existingTokenUsageObj = existingMetadata['tokenUsage'] as
          | Record<string, unknown>
          | undefined;
        const newTokenUsageObj = newMetadata['tokenUsage'] as Record<string, unknown> | undefined;

        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          metadata: {
            ...existingMetadata,
            ...newMetadata,
            // Merge latency object to preserve both values
            latency: {
              ...(existingLatencyObj || {}),
              ...(newLatencyObj || {}),
            },
            // Merge tokenUsage object to preserve both values
            tokenUsage: {
              ...(existingTokenUsageObj || {}),
              ...(newTokenUsageObj || {}),
            },
          },
        };
        return updated;
      });
    }
  }

  // =========================================================================
  // Message Building
  // =========================================================================

  /**
   * Convert a MessageBuilder to the final Message format.
   * This is called by the computed signal whenever the builder changes.
   */
  private buildMessage(builder: MessageBuilder): Message {
    // Sort content blocks by index and convert to final format
    const sortedBlocks = Array.from(builder.contentBlocks.entries())
      .sort(([a], [b]) => a - b)
      .map(([_, block]) => this.buildContentBlock(block));

    const message: Message = {
      id: builder.id,
      role: builder.role,
      content: sortedBlocks,
      created_at: builder.created_at,
      metadata: this.getMetadataForMessage(),
    };

    // Attach pending citations to assistant messages
    if (builder.role === 'assistant') {
      const citations = this.pendingCitations();
      if (citations.length > 0) {
        message.citations = citations;
      }
    }

    return message;
  }

  /**
   * Transform MetadataEvent from stream to API metadata format.
   * Converts usage and metrics to match the backend API structure.
   */
  private getMetadataForMessage(): Record<string, unknown> | null {
    const metadataEvent = this.metadataSignal();
    if (!metadataEvent) {
      return null;
    }

    const result: Record<string, unknown> = {};

    // Transform usage to tokenUsage format
    if (metadataEvent.usage) {
      result['tokenUsage'] = {
        inputTokens: metadataEvent.usage.inputTokens,
        outputTokens: metadataEvent.usage.outputTokens,
        totalTokens: metadataEvent.usage.totalTokens,
        ...(metadataEvent.usage.cacheReadInputTokens !== undefined && {
          cacheReadInputTokens: metadataEvent.usage.cacheReadInputTokens,
        }),
        ...(metadataEvent.usage.cacheWriteInputTokens !== undefined && {
          cacheWriteInputTokens: metadataEvent.usage.cacheWriteInputTokens,
        }),
      };
    }

    // Transform metrics to latency format
    if (metadataEvent.metrics) {
      result['latency'] = {
        timeToFirstToken: metadataEvent.metrics.timeToFirstByteMs ?? 0,
        endToEndLatency: metadataEvent.metrics.latencyMs,
      };
    }

    // Include cost if available (sent from backend during streaming)
    if (metadataEvent.cost !== undefined) {
      result['cost'] = metadataEvent.cost;
    }

    // Preserve any other fields (like trace)
    if (metadataEvent.trace !== undefined) {
      result['trace'] = metadataEvent.trace;
    }

    // Return null if no metadata was added
    return Object.keys(result).length > 0 ? result : null;
  }

  /**
   * Convert a ContentBlockBuilder to the final ContentBlock format.
   */
  private buildContentBlock(builder: ContentBlockBuilder): ContentBlock {
    // Handle reasoning content blocks
    if (builder.type === 'reasoningContent') {
      const reasoningText = builder.reasoningChunks.join('');
      return {
        type: 'reasoningContent',
        reasoningContent: {
          reasoningText: {
            text: reasoningText,
            // Note: signature is not available during streaming, only from saved messages
          },
        },
      } as ContentBlock;
    }

    // Handle tool use blocks
    if (builder.type === 'tool_use' || builder.type === 'toolUse') {
      const inputStr = builder.inputChunks.join('');
      let parsedInput: Record<string, unknown> = {};

      try {
        if (inputStr) {
          parsedInput = JSON.parse(inputStr);
        }
      } catch (e) {
        // Input might be incomplete during streaming
        // If we're finalizing and JSON is still invalid, log error but don't fail
        const errorMsg = e instanceof Error ? e.message : 'Unknown JSON parse error';

        // Set error if this is a finalized block with invalid JSON
        if (builder.isComplete) {
          this.setError(
            `Failed to parse tool input JSON for tool '${builder.toolName || 'unknown'}': ${errorMsg}`,
          );
        }
      }

      // Validate required fields
      if (!builder.toolUseId && builder.isComplete) {
        this.setError(`Tool use block missing toolUseId`);
      }

      if (!builder.toolName && builder.isComplete) {
        this.setError(`Tool use block missing toolName`);
      }

      // Build tool use data with result if available
      const toolUseData: any = {
        toolUseId: builder.toolUseId || uuidv4(),
        name: builder.toolName || 'unknown',
        input: parsedInput,
      };

      // Include result if available
      if (builder.result) {
        toolUseData.result = builder.result;
      }

      // Include status if available
      if (builder.status) {
        toolUseData.status = builder.status;
      }

      return {
        type: 'toolUse',
        toolUse: toolUseData,
      } as ContentBlock;
    }

    // Handle text blocks (default)
    return {
      type: 'text',
      text: builder.textChunks.join(''),
    } as ContentBlock;
  }

  /**
   * Move current message to completed messages.
   */
  private finalizeCurrentMessage(): void {
    const builder = this.currentMessageBuilder();
    if (!builder) return;

    const message = this.buildMessage(builder);

    // Only add non-empty messages
    if (message.content.length > 0) {
      this.completedMessages.update((messages) => [...messages, message]);
    }

    // Clear pending citations after attaching to message
    if (builder.role === 'assistant') {
      this.pendingCitations.set([]);
    }

    this.currentMessageBuilder.set(null);
  }
}
