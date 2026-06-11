// services/stream-parser.service.ts
import { Injectable, signal, computed, inject } from '@angular/core';
import { Message, ContentBlock, Citation } from '../models/message.model';
import { MetadataEvent } from '../models/content-types';
import { ChatStateService } from './chat-state.service';
import { v4 as uuidv4 } from 'uuid';
import {
  ErrorService,
  ErrorCode,
  StreamErrorEvent,
  ConversationalStreamError,
} from '../../../services/error/error.service';
import {
  QuotaWarningService,
  QuotaWarning,
  QuotaExceeded,
} from '../../../services/quota/quota-warning.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { CompactionSummaryService } from './compaction-summary.service';
import { ArtifactStateService } from '../artifacts/artifact-state.service';
import { McpAppStateService } from '../mcp-apps/mcp-app-state.service';
import type {
  OAuthRequiredEvent,
  ToolApprovalRequiredEvent,
  CompactionEvent,
  ArtifactEvent,
  UiResourceEvent,
  ToolInputPartialEvent,
} from '../../../shared/utils/stream-parser';
import {
  processStreamEvent,
  createStreamLineParser,
  inferContentBlockType,
  extractStreamingStringField,
  parseToolResultContent,
  type StreamParserCallbacks,
  type ContentBlockBuilder,
  type MessageBuilder,
  type ToolProgress,
  type ContentBlockDeltaEvent,
  type ContentBlockStartEvent,
  type ToolResultEventData,
} from '../../../shared/utils/stream-parser';

/**
 * Stream state tracking
 */
enum StreamState {
  Idle = 'idle',
  Streaming = 'streaming',
  Completed = 'completed',
  Error = 'error',
}

// Re-export ToolProgress for backwards compatibility
export type { ToolProgress };

/**
 * Tools whose `content` input is a long document worth surfacing live (as a
 * "generating…" preview) while the model is still streaming the tool call.
 */
const STREAMING_CONTENT_TOOLS = new Set(['create_artifact', 'update_artifact']);

@Injectable({
  providedIn: 'root',
})
export class StreamParserService {
  private chatStateService = inject(ChatStateService);
  private errorService = inject(ErrorService);
  private quotaWarningService = inject(QuotaWarningService);
  private oauthConsentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);
  private compactionSummary = inject(CompactionSummaryService);
  private artifactState = inject(ArtifactStateService);
  private mcpAppState = inject(McpAppStateService);

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
  // Private State
  // =========================================================================

  /** Current stream ID - prevents race conditions from overlapping streams */
  private currentStreamId: string | null = null;

  /** Current stream state */
  private streamState: StreamState = StreamState.Idle;

  /** Line parser for raw SSE lines */
  private lineParser = createStreamLineParser(this.createCallbacks());

  // =========================================================================
  // Public API
  // =========================================================================

  /**
   * Parse an incoming SSE line and update state.
   * Handles the event: and data: format from SSE.
   */
  parseSSELine(line: string): void {
    // Check if we should process events
    if (!this.shouldProcessEvent()) {
      return;
    }

    this.lineParser.parseLine(line);
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

    // Check if we should process this event
    // oauth_required arrives after message_stop/done by design (see CLAUDE.md SSE
    // table) — allow it through even when the stream state is Completed.
    // stream_error is a terminal signal that likewise arrives after
    // message_stop (e.g. max_tokens truncation) and must never be dropped by
    // state gating, or recovery affordances (Continue) silently disappear.
    const isAlwaysAllowedEvent =
      event === 'message_start' ||
      event === 'error' ||
      event === 'oauth_required' ||
      event === 'stream_error';
    if (!isAlwaysAllowedEvent && !this.shouldProcessEvent()) {
      return;
    }

    // Special handling for 'done' event which may have null/undefined data
    if (data === undefined || data === null) {
      if (event === 'done') {
        processStreamEvent(event, data, this.createCallbacks());
        return;
      }
      this.setError(`parseEventSourceMessage: data cannot be null/undefined for event '${event}'`);
      return;
    }

    processStreamEvent(event, data, this.createCallbacks());
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

    // Reset line parser
    this.lineParser.reset();
  }

  /**
   * Get the current stream ID (for debugging/monitoring).
   */
  getCurrentStreamId(): string | null {
    return this.currentStreamId;
  }

  /**
   * Get completed messages and clear them (for persisting to backend).
   */
  flushCompletedMessages(): Message[] {
    const messages = this.completedMessages();
    this.completedMessages.set([]);
    return messages;
  }

  // =========================================================================
  // Callbacks Factory
  // =========================================================================

  /**
   * Create callbacks for the stream parser core.
   * These callbacks wire the pure parsing logic to our state management.
   */
  private createCallbacks(): StreamParserCallbacks {
    return {
      onMessageStart: (data) => this.handleMessageStart(data),
      onMessageStop: (data) => this.handleMessageStop(data),
      onDone: () => this.handleDone(),

      onContentBlockStart: (data) => this.handleContentBlockStart(data),
      onContentBlockDelta: (data) => this.handleContentBlockDelta(data),
      onContentBlockStop: (data) => this.handleContentBlockStop(data),

      onToolUse: (data) => this.handleToolUseProgress(data),
      onToolResult: (data) => this.handleToolResult(data),
      onToolProgress: (progress) => this.toolProgressSignal.set(progress),

      onMetadata: (data) => this.handleMetadata(data),
      onReasoning: (data) => this.handleReasoning(data),
      onCitation: (data) => this.handleCitation(data),

      onQuotaWarning: (data) => this.quotaWarningService.setWarning(data as QuotaWarning),
      onQuotaExceeded: (data) => this.quotaWarningService.setQuotaExceeded(data as QuotaExceeded),

      onOAuthRequired: (data: OAuthRequiredEvent) => {
        // oauth_required arrives after message_stop, so the triggering
        // assistant message is normally in completedMessages; fall back
        // to the in-flight builder for tool_use stop reasons that keep
        // the message active.
        const messages = this.allMessages();
        let lastAssistantId: string | undefined;
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === 'assistant') {
            lastAssistantId = messages[i].id;
            break;
          }
        }
        this.oauthConsentService.requestConsent(
          data.providerId,
          data.authorizationUrl,
          data.interruptId,
          lastAssistantId,
          this.sessionId ?? undefined,
        );
      },

      onCompaction: (data: CompactionEvent) => this.compactionSummary.recordLive(data),

      onArtifact: (data: ArtifactEvent) => {
        // Same post-message_stop timing as oauth_required: the producing
        // assistant message is the last assistant message in the list.
        // Anchor live placement to its concrete id — the numeric index
        // only lines up after a reload (it counts the memory tool
        // messages the folded client message doesn't have).
        const messages = this.allMessages();
        let lastAssistantId: string | undefined;
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === 'assistant') {
            lastAssistantId = messages[i].id;
            break;
          }
        }
        this.artifactState.recordLive(data, lastAssistantId);
      },

      onUiResource: (data: UiResourceEvent) => {
        // Inline event (arrives right after its tool_result, mid-stream),
        // unlike the post-message_stop side channels above — just record
        // it keyed by toolUseId. The tool-use renderer picks it up
        // reactively and swaps in the MCP App frame.
        this.mcpAppState.recordLive(data);
      },

      onToolInputPartial: (data: ToolInputPartialEvent) => {
        // Streamed partial tool input (SEP-1865). Arrives repeatedly while a
        // UI tool's args are still streaming (after early frame mount). Record
        // the latest healed prefix keyed by toolUseId; the frame relays it to
        // the App as `ui/notifications/tool-input-partial` for progressive
        // rendering (e.g. Excalidraw's guided camera tour).
        this.mcpAppState.recordPartialInput(data.toolUseId, data.arguments);
      },

      onToolApprovalRequired: (data: ToolApprovalRequiredEvent) => {
        const messages = this.allMessages();
        let lastAssistantId: string | undefined;
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === 'assistant') {
            lastAssistantId = messages[i].id;
            break;
          }
        }
        this.toolApprovalService.requestApproval({
          interruptId: data.interruptId,
          toolUseId: data.toolUseId,
          toolName: data.toolName,
          toolInput: data.toolInput ?? undefined,
          message: data.message,
          messageId: lastAssistantId,
          sessionId: this.sessionId ?? undefined,
        });
      },

      onError: (data) => this.handleError(data),
      onStreamError: (data) => {
        const streamError = data as ConversationalStreamError;
        this.errorService.handleConversationalStreamError(streamError);
        // A max_tokens truncation is recoverable: Strands already persisted
        // the partial assistant turn, so the user can continue from it.
        // Surface the "Continue" affordance on the last assistant message.
        const isMaxTokens =
          streamError.code === ErrorCode.MAX_TOKENS ||
          streamError.metadata?.['error_kind'] === 'max_tokens';
        if (isMaxTokens) {
          this.chatStateService.setLastTurnContinuable(true);
        }
      },

      onParseError: (message) => this.setError(message),
    };
  }

  // =========================================================================
  // Event Handlers
  // =========================================================================

  private handleMessageStart(data: { role: 'user' | 'assistant' }): void {
    // Initialize stream ID if not set
    if (!this.currentStreamId) {
      this.currentStreamId = uuidv4();
    }

    // Update stream state
    this.streamState = StreamState.Streaming;

    // Clear any previous errors
    this.errorSignal.set(null);

    // If there's an existing message, finalize it before starting a new one
    const currentBuilder = this.currentMessageBuilder();
    if (currentBuilder) {
      this.finalizeCurrentMessage();
    }

    // Clear stopReason in ChatStateService
    this.chatStateService.setStopReason(null);

    // A new assistant turn is streaming — retire any stale "Continue"
    // affordance from a previous max_tokens truncation.
    this.chatStateService.setLastTurnContinuable(false);

    // Compute predictable message ID
    const completedCount = this.completedMessages().length;
    const messageIndex = this.startingMessageCount + completedCount;
    const computedId = this.sessionId ? `msg-${this.sessionId}-${messageIndex}` : uuidv4();

    // Create new message builder
    const builder: MessageBuilder = {
      id: computedId,
      role: data.role,
      contentBlocks: new Map(),
      createdAt: new Date().toISOString(),
      isComplete: false,
    };

    this.currentMessageBuilder.set(builder);
  }

  private handleContentBlockStart(data: ContentBlockStartEvent): void {
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError('content_block_start: received without active message');
      return;
    }

    if (currentBuilder.contentBlocks.has(data.contentBlockIndex)) {
      this.setError(`content_block_start: block at index ${data.contentBlockIndex} already exists`);
      return;
    }

    const blockType: 'text' | 'tool_use' = data.type === 'tool_use' ? 'tool_use' : 'text';

    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      const blockBuilder: ContentBlockBuilder = {
        index: data.contentBlockIndex,
        type: blockType,
        textChunks: [],
        inputChunks: [],
        reasoningChunks: [],
        toolUseId: data.toolUse?.toolUseId,
        toolName: data.toolUse?.name,
        isComplete: false,
      };

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(data.contentBlockIndex, blockBuilder);

      return { ...builder, contentBlocks: newBlocks };
    });

    // Show tool progress for tool_use blocks
    if (blockType === 'tool_use' && data.toolUse) {
      this.toolProgressSignal.set({
        visible: true,
        toolName: data.toolUse.name,
        toolUseId: data.toolUse.toolUseId,
        message: `Running ${data.toolUse.name}...`,
        startTime: Date.now(),
      });
    }
  }

  private handleContentBlockDelta(data: ContentBlockDeltaEvent): void {
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError('content_block_delta: received without active message');
      return;
    }

    const inferredType = inferContentBlockType(data);

    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      let block = builder.contentBlocks.get(data.contentBlockIndex);

      // Auto-create block if it doesn't exist (Claude skips content_block_start for text)
      if (!block) {
        block = {
          index: data.contentBlockIndex,
          type: inferredType,
          textChunks: [],
          inputChunks: [],
          reasoningChunks: [],
          isComplete: false,
        };
      }

      // Upgrade block type if needed
      if (block.type === 'text' && inferredType === 'tool_use') {
        block.type = 'tool_use';
      }

      // Update chunks
      if (data.text !== undefined) {
        if (typeof data.text !== 'string') {
          this.setError(`content_block_delta: text must be string, got ${typeof data.text}`);
          return builder;
        }
        block.textChunks.push(data.text);
      }

      if (data.input !== undefined) {
        if (typeof data.input !== 'string') {
          this.setError(`content_block_delta: input must be string, got ${typeof data.input}`);
          return builder;
        }
        block.inputChunks.push(data.input);
      }

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(data.contentBlockIndex, { ...block });

      return { ...builder, contentBlocks: newBlocks };
    });
  }

  private handleContentBlockStop(data: { contentBlockIndex: number }): void {
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError('content_block_stop: received without active message');
      return;
    }

    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      const block = builder.contentBlocks.get(data.contentBlockIndex);
      if (!block) {
        this.setError(`content_block_stop: block at index ${data.contentBlockIndex} does not exist`);
        return builder;
      }

      if (block.isComplete) {
        return builder; // Idempotent
      }

      block.isComplete = true;

      if (block.type === 'tool_use') {
        this.toolProgressSignal.set({ visible: false });
      }

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(data.contentBlockIndex, { ...block });

      return { ...builder, contentBlocks: newBlocks };
    });
  }

  private handleToolUseProgress(data: {
    tool_use: { name: string; tool_use_id: string; input: string };
  }): void {
    this.toolProgressSignal.update((progress) => ({
      ...progress,
      visible: true,
      toolName: data.tool_use.name,
      toolUseId: data.tool_use.tool_use_id,
    }));
  }

  private handleToolResult(data: ToolResultEventData): void {
    const toolUseId = data.tool_result.toolUseId;
    const content = data.tool_result.content || [];
    const status = data.tool_result.status || 'success';

    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError('tool_result: received without active message');
      return;
    }

    // Find the tool_use block
    let foundIndex: number | null = null;
    for (const [index, block] of currentBuilder.contentBlocks.entries()) {
      if (
        (block.type === 'tool_use' || block.type === 'toolUse') &&
        block.toolUseId === toolUseId
      ) {
        foundIndex = index;
        break;
      }
    }

    if (foundIndex === null) {
      return; // Tool use block not found
    }

    const resultContent = parseToolResultContent(content);

    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      const block = builder.contentBlocks.get(foundIndex!);
      if (!block) return builder;

      const updatedBlock: ContentBlockBuilder = {
        ...block,
        result: {
          content: resultContent,
          status: status,
        },
        status: status === 'error' ? 'error' : 'complete',
      };

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(foundIndex!, updatedBlock);

      return { ...builder, contentBlocks: newBlocks };
    });

    this.toolProgressSignal.set({ visible: false });
  }

  private handleMessageStop(data: { stopReason: string }): void {
    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError('message_stop: received without active message');
      return;
    }

    this.chatStateService.setStopReason(data.stopReason);

    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;
      return { ...builder, isComplete: true };
    });

    // If stop reason is tool_use, keep message active for tool result
    if (data.stopReason !== 'tool_use') {
      this.finalizeCurrentMessage();
    }
  }

  private handleDone(): void {
    this.finalizeCurrentMessage();
    this.isStreamCompleteSignal.set(true);
    this.toolProgressSignal.set({ visible: false });
    this.streamState = StreamState.Completed;

    // Automatic cleanup after delay
    setTimeout(() => {
      if (this.streamState === StreamState.Completed) {
        this.flushCompletedMessages();
      }
    }, 5000);
  }

  private handleError(data: unknown): void {
    let errorMessage = 'Unknown error';

    if (data && typeof data === 'object') {
      const potentialError = data as Partial<StreamErrorEvent>;

      if (potentialError.error && potentialError.code) {
        const streamError: StreamErrorEvent = {
          error: potentialError.error,
          code: potentialError.code,
          detail: potentialError.detail,
          recoverable: potentialError.recoverable ?? false,
          metadata: potentialError.metadata,
        };

        this.errorService.handleStreamError(streamError);
        errorMessage = streamError.error;
      } else {
        const errorData = data as { error?: string; message?: string };
        errorMessage = errorData.error || errorData.message || errorMessage;
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

  private handleMetadata(data: MetadataEvent): void {
    if (!data.usage && !data.metrics) {
      return;
    }

    this.metadataSignal.set(data);
    this.updateLastCompletedMessageWithMetadata();

    // Drive the session cost + context badge above the composer.
    // Cost on the wire may be either a number (legacy) or a CostBreakdown
    // object — extract the total either way (matches backend's Union shape).
    const turnCost = typeof data.cost === 'number' ? data.cost : data.cost?.total ?? 0;
    if (turnCost > 0) {
      this.chatStateService.addTurnCost(turnCost);
    }

    // Only update the context badge from the *final* metadata event —
    // the synthesized one the stream coordinator emits right before
    // `done`. Strands fires a `metadata` event per LLM call within a
    // turn; intermediate events carry per-call usage (sometimes with
    // missing or zero cache fields) that would make the badge collapse
    // mid-turn. The final event is the only one that carries
    // `contextWindow`, so we use that as the gate.
    //
    // Sum all three usage buckets: `inputTokens` is uncached input
    // only, `cacheReadInputTokens` is the cached prefix, and
    // `cacheWriteInputTokens` is freshly-cached content. Together they
    // represent true context-window occupancy.
    const usage = data.usage;
    if (data.contextWindow && usage && typeof usage.inputTokens === 'number') {
      const totalContext =
        usage.inputTokens +
        (usage.cacheReadInputTokens ?? 0) +
        (usage.cacheWriteInputTokens ?? 0);
      this.chatStateService.setContext(totalContext, data.contextWindow);
    }
  }

  private handleReasoning(data: { reasoningText?: string }): void {
    if (!data.reasoningText) {
      return;
    }

    const currentBuilder = this.currentMessageBuilder();
    if (!currentBuilder) {
      return;
    }

    this.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      // Find or create reasoning block
      let reasoningBlock: ContentBlockBuilder | undefined;
      let reasoningIndex: number = -1;

      for (const [index, block] of builder.contentBlocks.entries()) {
        if (block.type === 'reasoningContent') {
          reasoningBlock = block;
          reasoningIndex = index;
          break;
        }
      }

      if (!reasoningBlock) {
        const maxIndex = Math.max(-1, ...Array.from(builder.contentBlocks.keys()));
        reasoningIndex = maxIndex + 1;

        reasoningBlock = {
          index: reasoningIndex,
          type: 'reasoningContent',
          textChunks: [],
          inputChunks: [],
          reasoningChunks: [],
          isComplete: false,
        };
      }

      reasoningBlock.reasoningChunks.push(data.reasoningText!);

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(reasoningIndex, { ...reasoningBlock });

      return { ...builder, contentBlocks: newBlocks };
    });
  }

  private handleCitation(data: Citation): void {
    this.pendingCitations.update((citations) => [
      ...citations,
      {
        assistantId: data.assistantId,
        documentId: data.documentId,
        fileName: data.fileName,
        text: data.text,
      },
    ]);
  }

  // =========================================================================
  // Helper Methods
  // =========================================================================

  private shouldProcessEvent(): boolean {
    if (!this.currentStreamId) {
      return true; // Allow first event
    }

    if (this.streamState === StreamState.Completed || this.streamState === StreamState.Error) {
      return false;
    }

    return true;
  }

  private setError(message: string): void {
    this.errorSignal.set(message);
    this.isStreamCompleteSignal.set(true);
    this.toolProgressSignal.set({ visible: false });
    this.streamState = StreamState.Error;
  }

  private updateLastCompletedMessageWithMetadata(): void {
    const completed = this.completedMessages();
    if (completed.length === 0) return;

    const lastMessage = completed[completed.length - 1];
    const newMetadata = this.getMetadataForMessage();
    if (!newMetadata) return;

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

    // Check if we need to update
    const existingMetadata = lastMessage.metadata as Record<string, unknown>;
    const existingLatency = existingMetadata['latency'] as { timeToFirstToken?: number | null } | undefined;
    const existingTTFT = existingLatency?.timeToFirstToken;
    const existingCost = existingMetadata['cost'] as number | undefined;
    const existingTokenUsage = existingMetadata['tokenUsage'] as {
      cacheReadInputTokens?: number;
      cacheWriteInputTokens?: number;
    } | undefined;

    const newLatency = newMetadata['latency'] as { timeToFirstToken?: number | null } | undefined;
    const newTTFT = newLatency?.timeToFirstToken;
    const newCost = newMetadata['cost'] as number | undefined;
    const newTokenUsage = newMetadata['tokenUsage'] as {
      cacheReadInputTokens?: number;
      cacheWriteInputTokens?: number;
    } | undefined;

    const existingBreakdown = existingMetadata['contextBreakdown'];
    const newBreakdown = newMetadata['contextBreakdown'];

    const needsUpdate =
      (!existingTTFT && newTTFT) ||
      (existingCost === undefined && newCost !== undefined) ||
      (existingTokenUsage?.cacheReadInputTokens === undefined &&
        newTokenUsage?.cacheReadInputTokens !== undefined) ||
      (existingTokenUsage?.cacheWriteInputTokens === undefined &&
        newTokenUsage?.cacheWriteInputTokens !== undefined) ||
      (existingBreakdown === undefined && newBreakdown !== undefined);

    if (needsUpdate) {
      this.completedMessages.update((messages) => {
        const updated = [...messages];
        const existingLatencyObj = existingMetadata['latency'] as Record<string, unknown> | undefined;
        const newLatencyObj = newMetadata['latency'] as Record<string, unknown> | undefined;
        const existingTokenUsageObj = existingMetadata['tokenUsage'] as Record<string, unknown> | undefined;
        const newTokenUsageObj = newMetadata['tokenUsage'] as Record<string, unknown> | undefined;

        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          metadata: {
            ...existingMetadata,
            ...newMetadata,
            latency: { ...(existingLatencyObj || {}), ...(newLatencyObj || {}) },
            tokenUsage: { ...(existingTokenUsageObj || {}), ...(newTokenUsageObj || {}) },
          },
        };
        return updated;
      });
    }
  }

  // =========================================================================
  // Message Building
  // =========================================================================

  private buildMessage(builder: MessageBuilder): Message {
    const sortedBlocks = Array.from(builder.contentBlocks.entries())
      .sort(([a], [b]) => a - b)
      .map(([_, block]) => this.buildContentBlock(block));

    const message: Message = {
      id: builder.id,
      role: builder.role,
      content: sortedBlocks,
      createdAt: builder.createdAt,
      metadata: this.getMetadataForMessage(),
    };

    if (builder.role === 'assistant') {
      const citations = this.pendingCitations();
      if (citations.length > 0) {
        message.citations = citations;
      }
    }

    return message;
  }

  private getMetadataForMessage(): Record<string, unknown> | null {
    const metadataEvent = this.metadataSignal();
    if (!metadataEvent) {
      return null;
    }

    const result: Record<string, unknown> = {};

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

    if (metadataEvent.metrics) {
      // Preserve `null` for unmeasured TTFT instead of coercing to 0 — a
      // real time-to-first-token can never be 0ms, and the badge below
      // already hides itself for null/undefined/0 via a truthy check.
      result['latency'] = {
        timeToFirstToken: metadataEvent.metrics.timeToFirstByteMs ?? null,
        endToEndLatency: metadataEvent.metrics.latencyMs,
      };
    }

    if (metadataEvent.cost !== undefined) {
      result['cost'] = metadataEvent.cost;
    }

    if (metadataEvent.contextBreakdown !== undefined) {
      result['contextBreakdown'] = metadataEvent.contextBreakdown;
    }

    if (metadataEvent.trace !== undefined) {
      result['trace'] = metadataEvent.trace;
    }

    return Object.keys(result).length > 0 ? result : null;
  }

  private buildContentBlock(builder: ContentBlockBuilder): ContentBlock {
    // Handle reasoning content blocks
    if (builder.type === 'reasoningContent') {
      return {
        type: 'reasoningContent',
        reasoningContent: {
          reasoningText: {
            text: builder.reasoningChunks.join(''),
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
        if (builder.isComplete) {
          const errorMsg = e instanceof Error ? e.message : 'Unknown JSON parse error';
          this.setError(`Failed to parse tool input JSON for '${builder.toolName}': ${errorMsg}`);
        }
      }

      const toolUseData: Record<string, unknown> = {
        toolUseId: builder.toolUseId || uuidv4(),
        name: builder.toolName || 'unknown',
        input: parsedInput,
      };

      if (builder.result) {
        toolUseData['result'] = builder.result;
      }

      if (builder.status) {
        toolUseData['status'] = builder.status;
      }

      // While an artifact tool is still streaming (no result yet), surface the
      // partially-generated `content` so the UI can show live progress. The
      // full tool-input JSON is incomplete during this window, so JSON.parse
      // above yields {} — we extract the in-flight value directly instead.
      if (
        !builder.result &&
        builder.toolName &&
        STREAMING_CONTENT_TOOLS.has(builder.toolName)
      ) {
        const streaming = extractStreamingStringField(inputStr, 'content');
        if (streaming) {
          toolUseData['streamingContent'] = streaming;
        }
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

  private finalizeCurrentMessage(): void {
    const builder = this.currentMessageBuilder();
    if (!builder) return;

    const message = this.buildMessage(builder);

    if (message.content.length > 0) {
      this.completedMessages.update((messages) => [...messages, message]);
    }

    if (builder.role === 'assistant') {
      this.pendingCitations.set([]);
    }

    this.currentMessageBuilder.set(null);
  }
}
