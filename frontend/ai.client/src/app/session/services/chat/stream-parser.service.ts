// services/stream-parser.service.ts
import { Injectable, Signal, WritableSignal, signal, computed, inject } from '@angular/core';
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
  QuotaSessionNotice,
  QuotaExceeded,
} from '../../../services/quota/quota-warning.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { CompactionSummaryService } from './compaction-summary.service';
import { ArtifactStateService } from '../artifacts/artifact-state.service';
import { McpAppStateService } from '../mcp-apps/mcp-app-state.service';
import { SessionService } from '../session/session.service';
import type {
  OAuthRequiredEvent,
  ToolApprovalRequiredEvent,
  CompactionEvent,
  ArtifactEvent,
  UiResourceEvent,
  ToolInputPartialEvent,
  SessionTitleEvent,
  ModelRetryEvent,
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

/**
 * All mutable parse state for one session's stream. Each session gets its
 * own instance so two conversations can stream concurrently without one
 * stream's events corrupting the other's message builders or identity.
 */
interface ParserSessionState {
  /** Session this state belongs to (used for message IDs and side channels). */
  sessionId: string;

  /** Starting message count for ID computation */
  startingMessageCount: number;

  /**
   * Current stream ID. Regenerated on every reset — a stream captures it at
   * start and passes it back with each event so late events from a
   * superseded stream (same session, e.g. a re-submit) are dropped.
   */
  currentStreamId: string;

  /** Current stream state */
  streamState: StreamState;

  /** The current message being streamed */
  currentMessageBuilder: WritableSignal<MessageBuilder | null>;

  /** Completed messages in the current turn (for multi-turn tool use) */
  completedMessages: WritableSignal<Message[]>;

  /** Tool progress indicator state */
  toolProgress: WritableSignal<ToolProgress>;

  /**
   * The most recent model-call retry this turn, or null. Set by the
   * `model_retry` SSE event and cleared as soon as content arrives, so the
   * loading indicator can explain the silence instead of leaving the user to
   * read it as a hang.
   */
  modelRetry: WritableSignal<ModelRetryEvent | null>;

  /** Error state */
  error: WritableSignal<string | null>;

  /** Stream completion state */
  isStreamComplete: WritableSignal<boolean>;

  /** Metadata (usage, metrics) from the stream */
  metadata: WritableSignal<MetadataEvent | null>;

  /** Pending citations for the next assistant message */
  pendingCitations: WritableSignal<Citation[]>;

  /** The current message converted to the final Message format. */
  currentMessage: Signal<Message | null>;

  /** All messages in this stream (completed + current). */
  allMessages: Signal<Message[]>;

  /** The ID of the message currently being streamed, or null. */
  streamingMessageId: Signal<string | null>;

  /** Callbacks wiring the pure parsing logic to this state's signals. */
  callbacks: StreamParserCallbacks;

  /** Line parser for raw SSE lines */
  lineParser: ReturnType<typeof createStreamLineParser>;
}

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
  private sessionService = inject(SessionService);

  // =========================================================================
  // Per-Session State
  // =========================================================================

  /**
   * Parser state per session, held in a signal so the cached per-session
   * accessor computeds below re-read when reset() swaps in a fresh state.
   */
  private readonly states = signal<ReadonlyMap<string, ParserSessionState>>(new Map());

  /** Stable per-session accessor signals (cached so callers can hold them). */
  private readonly allMessagesCache = new Map<string, Signal<Message[]>>();
  private readonly streamingMessageIdCache = new Map<string, Signal<string | null>>();
  private readonly toolProgressCache = new Map<string, Signal<ToolProgress>>();
  private readonly modelRetryCache = new Map<string, Signal<ModelRetryEvent | null>>();
  private readonly citationsCache = new Map<string, Signal<Citation[]>>();
  private readonly errorCache = new Map<string, Signal<string | null>>();
  private readonly isStreamCompleteCache = new Map<string, Signal<boolean>>();

  // =========================================================================
  // Public API
  // =========================================================================

  /**
   * All messages in a session's current streaming batch (completed +
   * current). This is what the message-map sync binds to for rendering.
   * Returns a stable signal per session; empty until the first reset().
   */
  allMessagesFor(sessionId: string): Signal<Message[]> {
    return this.cachedAccessor(this.allMessagesCache, sessionId, (state) => state.allMessages(), []);
  }

  /**
   * The ID of the message currently being streamed in a session, or null.
   * Used by UI components to determine which message should animate.
   */
  streamingMessageIdFor(sessionId: string): Signal<string | null> {
    return this.cachedAccessor(this.streamingMessageIdCache, sessionId, (state) => state.streamingMessageId(), null);
  }

  /** Tool progress indicator state for a session. */
  toolProgressFor(sessionId: string): Signal<ToolProgress> {
    return this.cachedAccessor(this.toolProgressCache, sessionId, (state) => state.toolProgress(), { visible: false });
  }

  /**
   * The current model-call retry notice for a session, or null when the model
   * is responding normally. Drives the "still working" copy on the loader.
   */
  modelRetryFor(sessionId: string): Signal<ModelRetryEvent | null> {
    return this.cachedAccessor(this.modelRetryCache, sessionId, (state) => state.modelRetry(), null);
  }

  /** Pending citations for a session's next assistant message. */
  citationsFor(sessionId: string): Signal<Citation[]> {
    return this.cachedAccessor(this.citationsCache, sessionId, (state) => state.pendingCitations(), []);
  }

  /** Parse-error state for a session. */
  errorFor(sessionId: string): Signal<string | null> {
    return this.cachedAccessor(this.errorCache, sessionId, (state) => state.error(), null);
  }

  /** Stream completion state for a session. */
  isStreamCompleteFor(sessionId: string): Signal<boolean> {
    return this.cachedAccessor(this.isStreamCompleteCache, sessionId, (state) => state.isStreamComplete(), false);
  }

  /**
   * Parse an incoming SSE line for a session and update its state.
   * Handles the event: and data: format from SSE.
   */
  parseSSELine(sessionId: string, line: string): void {
    const state = this.states().get(sessionId);
    if (!state || !this.shouldProcessEvent(state)) {
      return;
    }

    state.lineParser.parseLine(line);
  }

  /**
   * Parse a pre-parsed EventSourceMessage (from fetch-event-source).
   *
   * @param sessionId - The session whose stream produced the event
   * @param expectedStreamId - The stream ID captured when the request
   *   started (see {@link getCurrentStreamId}). If the session's parser has
   *   since been reset for a newer stream, the event is silently dropped —
   *   this is what keeps a superseded stream's late events from corrupting
   *   its replacement.
   */
  parseEventSourceMessage(
    sessionId: string,
    event: string,
    data: unknown,
    expectedStreamId?: string | null,
  ): void {
    const state = this.states().get(sessionId);
    if (!state) {
      return; // No active parser for this session — nothing to update.
    }

    if (expectedStreamId && state.currentStreamId !== expectedStreamId) {
      return; // Stale event from a superseded stream for this session.
    }

    // Validate inputs
    if (!event || typeof event !== 'string') {
      this.setError(state, 'parseEventSourceMessage: event must be a non-empty string');
      return;
    }

    // Check if we should process this event
    // oauth_required arrives after message_stop/done by design (see CLAUDE.md SSE
    // table) — allow it through even when the stream state is Completed.
    // stream_error is a terminal signal that likewise arrives after
    // message_stop (e.g. max_tokens truncation) and must never be dropped by
    // state gating, or recovery affordances (Continue) silently disappear.
    // session_title is a side-channel event interleaved between agent
    // events — it can land right after `done` when title generation
    // finishes late in the turn, and it never touches message builders,
    // so state gating must not drop it.
    const isAlwaysAllowedEvent =
      event === 'message_start' ||
      event === 'error' ||
      event === 'oauth_required' ||
      event === 'stream_error' ||
      event === 'session_title';
    if (!isAlwaysAllowedEvent && !this.shouldProcessEvent(state)) {
      return;
    }

    // Special handling for 'done' event which may have null/undefined data
    if (data === undefined || data === null) {
      if (event === 'done') {
        processStreamEvent(event, data, state.callbacks);
        return;
      }
      this.setError(state, `parseEventSourceMessage: data cannot be null/undefined for event '${event}'`);
      return;
    }

    processStreamEvent(event, data, state.callbacks);
  }

  /**
   * Reset a session's state for a new stream.
   * Generates a new stream ID to prevent race conditions.
   *
   * IMPORTANT: Call this before starting a new stream so events from that
   * session's previous stream (identified by their captured stream ID) are
   * dropped instead of interfering.
   *
   * @param sessionId - Session the stream belongs to (also used for computing
   *   predictable message IDs)
   * @param startingMessageCount - Current message count in the session (for ID computation)
   */
  reset(sessionId: string, startingMessageCount?: number): void {
    const state = this.createState(sessionId, startingMessageCount || 0);
    this.states.update((map) => {
      const next = new Map(map);
      next.set(sessionId, state);
      return next;
    });
  }

  /**
   * Get a session's current stream ID. Captured by ChatHttpService when a
   * request starts, and passed back with each event / lifecycle callback to
   * detect stale streams.
   */
  getCurrentStreamId(sessionId: string): string | null {
    return this.states().get(sessionId)?.currentStreamId ?? null;
  }

  /**
   * Get a session's completed messages and clear them.
   */
  flushCompletedMessages(sessionId: string): Message[] {
    const state = this.states().get(sessionId);
    if (!state) return [];
    const messages = state.completedMessages();
    state.completedMessages.set([]);
    return messages;
  }

  /**
   * Drop a session's parser state entirely (e.g. when the session is
   * cleared). Cached accessor signals keep working — they fall back to
   * their empty defaults.
   */
  clearSession(sessionId: string): void {
    if (!this.states().get(sessionId)) return;
    this.states.update((map) => {
      const next = new Map(map);
      next.delete(sessionId);
      return next;
    });
  }

  // =========================================================================
  // State Factory
  // =========================================================================

  private createState(sessionId: string, startingMessageCount: number): ParserSessionState {
    const currentMessageBuilder = signal<MessageBuilder | null>(null);
    const completedMessages = signal<Message[]>([]);
    const isStreamComplete = signal<boolean>(false);

    const state: ParserSessionState = {
      sessionId,
      startingMessageCount,
      currentStreamId: uuidv4(),
      streamState: StreamState.Idle,
      currentMessageBuilder,
      completedMessages,
      toolProgress: signal<ToolProgress>({ visible: false }),
      modelRetry: signal<ModelRetryEvent | null>(null),
      error: signal<string | null>(null),
      isStreamComplete,
      metadata: signal<MetadataEvent | null>(null),
      pendingCitations: signal<Citation[]>([]),
      currentMessage: computed<Message | null>(() => {
        const builder = currentMessageBuilder();
        return builder ? this.buildMessage(state, builder) : null;
      }),
      allMessages: computed<Message[]>(() => {
        const completed = completedMessages();
        const current = state.currentMessage();
        return current ? [...completed, current] : completed;
      }),
      streamingMessageId: computed<string | null>(() => {
        const builder = currentMessageBuilder();
        const isComplete = isStreamComplete();

        // Return the message ID if we have an active builder and stream is not complete
        if (builder && !isComplete) {
          return builder.id;
        }
        return null;
      }),
      callbacks: undefined as unknown as StreamParserCallbacks,
      lineParser: undefined as unknown as ReturnType<typeof createStreamLineParser>,
    };

    state.callbacks = this.createCallbacks(state);
    state.lineParser = createStreamLineParser(state.callbacks);
    return state;
  }

  /**
   * Cached per-session accessor: a stable computed that follows the
   * session's current state object across resets and falls back to a
   * default before the first reset.
   */
  private cachedAccessor<T>(
    cache: Map<string, Signal<T>>,
    sessionId: string,
    read: (state: ParserSessionState) => T,
    fallback: T,
  ): Signal<T> {
    let cached = cache.get(sessionId);
    if (!cached) {
      cached = computed(() => {
        const state = this.states().get(sessionId);
        return state ? read(state) : fallback;
      });
      cache.set(sessionId, cached);
    }
    return cached;
  }

  /**
   * Whether this stream's session is the one the user is currently viewing.
   * Conversation-view side channels (artifacts panel, MCP App frames,
   * compaction badge) are viewed-session-scoped: they reset on route change
   * and re-hydrate from the server, so a background stream must not push
   * into them while another conversation is on screen.
   */
  private isViewedSession(state: ParserSessionState): boolean {
    return this.chatStateService.viewedSessionId() === state.sessionId;
  }

  // =========================================================================
  // Callbacks Factory
  // =========================================================================

  /**
   * Create callbacks for the stream parser core.
   * These callbacks wire the pure parsing logic to one session's state.
   */
  private createCallbacks(state: ParserSessionState): StreamParserCallbacks {
    return {
      onMessageStart: (data) => this.handleMessageStart(state, data),
      onMessageStop: (data) => this.handleMessageStop(state, data),
      onDone: () => this.handleDone(state),

      onContentBlockStart: (data) => this.handleContentBlockStart(state, data),
      onContentBlockDelta: (data) => this.handleContentBlockDelta(state, data),
      onContentBlockStop: (data) => this.handleContentBlockStop(state, data),

      onToolUse: (data) => this.handleToolUseProgress(state, data),
      onToolResult: (data) => this.handleToolResult(state, data),
      onToolProgress: (progress) => state.toolProgress.set(progress),

      onModelRetry: (data: ModelRetryEvent) => {
        // Not viewed-session-scoped on purpose: this signal is read per
        // session id, so a background conversation's retry stays with that
        // conversation instead of leaking into the one on screen.
        state.modelRetry.set(data);
      },

      onMetadata: (data) => this.handleMetadata(state, data),
      onReasoning: (data) => this.handleReasoning(state, data),
      onCitation: (data) => this.handleCitation(state, data),

      onQuotaWarning: (data) => this.quotaWarningService.setWarning(data as QuotaWarning),
      onQuotaSessionNotice: (data) =>
        this.quotaWarningService.setSessionNotice(data as QuotaSessionNotice),
      onQuotaExceeded: (data) => this.quotaWarningService.setQuotaExceeded(data as QuotaExceeded),

      onOAuthRequired: (data: OAuthRequiredEvent) => {
        // oauth_required arrives after message_stop, so the triggering
        // assistant message is normally in completedMessages; fall back
        // to the in-flight builder for tool_use stop reasons that keep
        // the message active.
        const lastAssistantId = this.findLastAssistantId(state);
        this.oauthConsentService.requestConsent(
          data.providerId,
          data.authorizationUrl,
          data.interruptId,
          lastAssistantId,
          state.sessionId,
        );
      },

      onCompaction: (data: CompactionEvent) => {
        // CompactionSummaryService is viewed-session-scoped (reset on route
        // change, reseeded from session metadata) — a background stream's
        // compaction must not bump the viewed conversation's badge.
        if (this.isViewedSession(state)) {
          this.compactionSummary.recordLive(data);
        }
      },

      onArtifact: (data: ArtifactEvent) => {
        // Viewed-session only: recordLive auto-opens the artifact panel,
        // which must never happen for a conversation streaming in the
        // background. Navigating back re-hydrates artifacts from the
        // server, so the dropped event is recovered there.
        if (!this.isViewedSession(state)) {
          return;
        }
        // Same post-message_stop timing as oauth_required: the producing
        // assistant message is the last assistant message in the list.
        // Anchor live placement to its concrete id — the numeric index
        // only lines up after a reload (it counts the memory tool
        // messages the folded client message doesn't have).
        const lastAssistantId = this.findLastAssistantId(state);
        this.artifactState.recordLive(data, lastAssistantId);
      },

      onUiResource: (data: UiResourceEvent) => {
        // Inline event (arrives right after its tool_result, mid-stream),
        // unlike the post-message_stop side channels above — just record
        // it keyed by toolUseId. The tool-use renderer picks it up
        // reactively and swaps in the MCP App frame. Viewed-session only:
        // McpAppStateService is reset on route change, so recording for a
        // background conversation would be wiped before it could render.
        if (this.isViewedSession(state)) {
          this.mcpAppState.recordLive(data);
        }
      },

      onToolInputPartial: (data: ToolInputPartialEvent) => {
        // Streamed partial tool input (SEP-1865). Arrives repeatedly while a
        // UI tool's args are still streaming (after early frame mount). Record
        // the latest healed prefix keyed by toolUseId; the frame relays it to
        // the App as `ui/notifications/tool-input-partial` for progressive
        // rendering (e.g. Excalidraw's guided camera tour).
        if (this.isViewedSession(state)) {
          this.mcpAppState.recordPartialInput(data.toolUseId, data.arguments);
        }
      },

      onSessionTitle: (data: SessionTitleEvent) => {
        // Server-generated title, pushed mid-stream on the session's first
        // turn (concurrent with the pending response). Deliberately NOT
        // viewed-session-scoped: the sidebar row must rename even when the
        // conversation streams in the background; applyServerTitle only
        // touches the header when this session is the one being viewed.
        // Guard on the event's own sessionId (belt-and-braces with the
        // parser's per-session state).
        if (data.sessionId === state.sessionId) {
          this.sessionService.applyServerTitle(data.sessionId, data.title);
        }
      },

      onToolApprovalRequired: (data: ToolApprovalRequiredEvent) => {
        const lastAssistantId = this.findLastAssistantId(state);
        this.toolApprovalService.requestApproval({
          interruptId: data.interruptId,
          toolUseId: data.toolUseId,
          toolName: data.toolName,
          toolInput: data.toolInput ?? undefined,
          message: data.message,
          messageId: lastAssistantId,
          sessionId: state.sessionId,
        });
      },

      onError: (data) => this.handleError(state, data),
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
          this.chatStateService.setLastTurnContinuable(state.sessionId, true);
        }
      },

      onParseError: (message) => this.setError(state, message),
    };
  }

  private findLastAssistantId(state: ParserSessionState): string | undefined {
    const messages = state.allMessages();
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') {
        return messages[i].id;
      }
    }
    return undefined;
  }

  // =========================================================================
  // Event Handlers
  // =========================================================================

  private handleMessageStart(state: ParserSessionState, data: { role: 'user' | 'assistant' }): void {
    // Update stream state
    state.streamState = StreamState.Streaming;

    // Clear any previous errors
    state.error.set(null);

    // Content is arriving, so whatever retry we were explaining is over.
    state.modelRetry.set(null);

    // If there's an existing message, finalize it before starting a new one
    const currentBuilder = state.currentMessageBuilder();
    if (currentBuilder) {
      this.finalizeCurrentMessage(state);
    }

    // Clear stopReason in ChatStateService
    this.chatStateService.setStopReason(state.sessionId, null);

    // A new assistant turn is streaming — retire any stale "Continue"
    // affordance from a previous max_tokens truncation, and any interrupted
    // chip from a previous aborted turn.
    this.chatStateService.setLastTurnContinuable(state.sessionId, false);
    this.chatStateService.setLastTurnInterrupted(state.sessionId, false);

    // Compute predictable message ID
    const completedCount = state.completedMessages().length;
    const messageIndex = state.startingMessageCount + completedCount;
    const computedId = `msg-${state.sessionId}-${messageIndex}`;

    // Create new message builder
    const builder: MessageBuilder = {
      id: computedId,
      role: data.role,
      contentBlocks: new Map(),
      createdAt: new Date().toISOString(),
      isComplete: false,
    };

    state.currentMessageBuilder.set(builder);
  }

  private handleContentBlockStart(state: ParserSessionState, data: ContentBlockStartEvent): void {
    const currentBuilder = state.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(state, 'content_block_start: received without active message');
      return;
    }

    if (currentBuilder.contentBlocks.has(data.contentBlockIndex)) {
      this.setError(state, `content_block_start: block at index ${data.contentBlockIndex} already exists`);
      return;
    }

    const blockType: 'text' | 'tool_use' = data.type === 'tool_use' ? 'tool_use' : 'text';

    state.currentMessageBuilder.update((builder) => {
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
      state.toolProgress.set({
        visible: true,
        toolName: data.toolUse.name,
        toolUseId: data.toolUse.toolUseId,
        message: `Running ${data.toolUse.name}...`,
        startTime: Date.now(),
      });
    }
  }

  private handleContentBlockDelta(state: ParserSessionState, data: ContentBlockDeltaEvent): void {
    const currentBuilder = state.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(state, 'content_block_delta: received without active message');
      return;
    }

    const inferredType = inferContentBlockType(data);

    state.currentMessageBuilder.update((builder) => {
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
          this.setError(state, `content_block_delta: text must be string, got ${typeof data.text}`);
          return builder;
        }
        block.textChunks.push(data.text);
      }

      if (data.input !== undefined) {
        if (typeof data.input !== 'string') {
          this.setError(state, `content_block_delta: input must be string, got ${typeof data.input}`);
          return builder;
        }
        block.inputChunks.push(data.input);
      }

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(data.contentBlockIndex, { ...block });

      return { ...builder, contentBlocks: newBlocks };
    });
  }

  private handleContentBlockStop(state: ParserSessionState, data: { contentBlockIndex: number }): void {
    const currentBuilder = state.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(state, 'content_block_stop: received without active message');
      return;
    }

    state.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;

      const block = builder.contentBlocks.get(data.contentBlockIndex);
      if (!block) {
        this.setError(state, `content_block_stop: block at index ${data.contentBlockIndex} does not exist`);
        return builder;
      }

      if (block.isComplete) {
        return builder; // Idempotent
      }

      block.isComplete = true;

      if (block.type === 'tool_use') {
        state.toolProgress.set({ visible: false });
      }

      const newBlocks = new Map(builder.contentBlocks);
      newBlocks.set(data.contentBlockIndex, { ...block });

      return { ...builder, contentBlocks: newBlocks };
    });
  }

  private handleToolUseProgress(state: ParserSessionState, data: {
    tool_use: { name: string; tool_use_id: string; input: string };
  }): void {
    state.toolProgress.update((progress) => ({
      ...progress,
      visible: true,
      toolName: data.tool_use.name,
      toolUseId: data.tool_use.tool_use_id,
    }));
  }

  private handleToolResult(state: ParserSessionState, data: ToolResultEventData): void {
    const toolUseId = data.tool_result.toolUseId;
    const content = data.tool_result.content || [];
    const status = data.tool_result.status || 'success';

    const currentBuilder = state.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(state, 'tool_result: received without active message');
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

    state.currentMessageBuilder.update((builder) => {
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

    state.toolProgress.set({ visible: false });
  }

  private handleMessageStop(state: ParserSessionState, data: { stopReason: string }): void {
    const currentBuilder = state.currentMessageBuilder();
    if (!currentBuilder) {
      this.setError(state, 'message_stop: received without active message');
      return;
    }

    this.chatStateService.setStopReason(state.sessionId, data.stopReason);

    state.currentMessageBuilder.update((builder) => {
      if (!builder) return builder;
      return { ...builder, isComplete: true };
    });

    // If stop reason is tool_use, keep message active for tool result
    if (data.stopReason !== 'tool_use') {
      this.finalizeCurrentMessage(state);
    }
  }

  private handleDone(state: ParserSessionState): void {
    this.finalizeCurrentMessage(state);
    state.isStreamComplete.set(true);
    state.toolProgress.set({ visible: false });
    state.modelRetry.set(null);
    state.streamState = StreamState.Completed;

    // Automatic cleanup after delay. Guarded on the stream ID so a session
    // that started a new stream in the meantime isn't flushed mid-parse.
    const streamId = state.currentStreamId;
    setTimeout(() => {
      const current = this.states().get(state.sessionId);
      if (
        current === state &&
        current.currentStreamId === streamId &&
        current.streamState === StreamState.Completed
      ) {
        this.flushCompletedMessages(state.sessionId);
      }
    }, 5000);
  }

  private handleError(state: ParserSessionState, data: unknown): void {
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

    this.setError(state, `Stream error: ${errorMessage}`);
  }

  private handleMetadata(state: ParserSessionState, data: MetadataEvent): void {
    if (!data.usage && !data.metrics) {
      return;
    }

    state.metadata.set(data);
    this.updateLastCompletedMessageWithMetadata(state);

    // Drive the session cost + context badge above the composer.
    // Cost on the wire may be either a number (legacy) or a CostBreakdown
    // object — extract the total either way (matches backend's Union shape).
    const turnCost = typeof data.cost === 'number' ? data.cost : data.cost?.total ?? 0;
    if (turnCost > 0) {
      this.chatStateService.addTurnCost(state.sessionId, turnCost);
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
      this.chatStateService.setContext(state.sessionId, totalContext, data.contextWindow);
    }
  }

  private handleReasoning(state: ParserSessionState, data: { reasoningText?: string }): void {
    if (!data.reasoningText) {
      return;
    }

    const currentBuilder = state.currentMessageBuilder();
    if (!currentBuilder) {
      return;
    }

    state.currentMessageBuilder.update((builder) => {
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

  private handleCitation(state: ParserSessionState, data: Citation): void {
    state.pendingCitations.update((citations) => [
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

  private shouldProcessEvent(state: ParserSessionState): boolean {
    return state.streamState !== StreamState.Completed && state.streamState !== StreamState.Error;
  }

  private setError(state: ParserSessionState, message: string): void {
    state.error.set(message);
    state.isStreamComplete.set(true);
    state.toolProgress.set({ visible: false });
    state.streamState = StreamState.Error;
  }

  private updateLastCompletedMessageWithMetadata(state: ParserSessionState): void {
    const completed = state.completedMessages();
    if (completed.length === 0) return;

    const lastMessage = completed[completed.length - 1];
    const newMetadata = this.getMetadataForMessage(state);
    if (!newMetadata) return;

    if (!lastMessage.metadata) {
      state.completedMessages.update((messages) => {
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
      state.completedMessages.update((messages) => {
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

  private buildMessage(state: ParserSessionState, builder: MessageBuilder): Message {
    const sortedBlocks = Array.from(builder.contentBlocks.entries())
      .sort(([a], [b]) => a - b)
      .map(([_, block]) => this.buildContentBlock(state, block));

    const message: Message = {
      id: builder.id,
      role: builder.role,
      content: sortedBlocks,
      createdAt: builder.createdAt,
      metadata: this.getMetadataForMessage(state),
    };

    if (builder.role === 'assistant') {
      const citations = state.pendingCitations();
      if (citations.length > 0) {
        message.citations = citations;
      }
    }

    return message;
  }

  private getMetadataForMessage(state: ParserSessionState): Record<string, unknown> | null {
    const metadataEvent = state.metadata();
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

  private buildContentBlock(state: ParserSessionState, builder: ContentBlockBuilder): ContentBlock {
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
          this.setError(state, `Failed to parse tool input JSON for '${builder.toolName}': ${errorMsg}`);
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

  private finalizeCurrentMessage(state: ParserSessionState): void {
    const builder = state.currentMessageBuilder();
    if (!builder) return;

    const message = this.buildMessage(state, builder);

    if (message.content.length > 0) {
      state.completedMessages.update((messages) => [...messages, message]);
    }

    if (builder.role === 'assistant') {
      state.pendingCitations.set([]);
    }

    state.currentMessageBuilder.set(null);
  }
}
