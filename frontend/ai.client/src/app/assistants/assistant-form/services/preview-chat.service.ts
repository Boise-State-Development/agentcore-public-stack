import { Injectable, InjectionToken, inject, signal, computed } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import type { EventSourceMessage, FetchEventSourceInit } from '@microsoft/fetch-event-source';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ConfigService } from '../../../services/config.service';
import { ToolService } from '../../../services/tool/tool.service';
import {
  Message,
  type ContentBlock,
  type ToolUseData,
  type ToolResultContent,
} from '../../../session/services/models/message.model';
import { PREVIEW_SESSION_PREFIX } from '../../../shared/constants/session.constants';
import {
  processStreamEvent,
  type StreamParserCallbacks,
  type ContentBlockDeltaEvent,
  type ToolUseEvent,
  type ToolResultEventData,
} from '../../../shared/utils/stream-parser';

/**
 * Injection token for the SSE client. Lets specs swap in a mock without
 * relying on `vi.mock('@microsoft/fetch-event-source')` — that approach
 * raced with sibling specs that transitively import this module in the
 * Angular vitest builder's shared worker pool, surfacing as the well-known
 * `expected vi.fn() called 1, got 0` flake on `preview-chat.service.spec.ts`.
 */
export type FetchEventSource = (
  input: RequestInfo,
  init: FetchEventSourceInit,
) => Promise<void>;

export const FETCH_EVENT_SOURCE = new InjectionToken<FetchEventSource>('FETCH_EVENT_SOURCE', {
  providedIn: 'root',
  factory: () => fetchEventSource,
});

/**
 * Component-scoped service for managing preview chat state.
 *
 * This service maintains its own isolated state separate from the global ChatStateService.
 * It uses the shared stream-parser-core for SSE parsing, but manages its own state via
 * callbacks. This avoids duplicating parsing logic while keeping state isolated.
 *
 * Preview sessions use the `preview-{uuid}` session ID format which the backend recognizes
 * and skips persistence for.
 *
 * The preview streams text and tool use so owners can test the assistant exactly as a
 * consumer would experience it (assistants can use the owner's enabled tools). Citations
 * and other advanced features remain out of scope for the preview.
 */
@Injectable()
export class PreviewChatService {
  private bffSession = inject(BffSessionService);
  private config = inject(ConfigService);
  private fetchEventSource = inject(FETCH_EVENT_SOURCE);
  private toolService = inject(ToolService);

  // Local state signals (isolated from global ChatStateService)
  private readonly messagesSignal = signal<Message[]>([]);
  private readonly loadingSignal = signal<boolean>(false);
  private readonly streamingMessageIdSignal = signal<string | null>(null);
  private readonly sessionIdSignal = signal<string>(`${PREVIEW_SESSION_PREFIX}${uuidv4()}`);
  private readonly errorSignal = signal<string | null>(null);

  // Abort controller for cancellation
  private abortController: AbortController | null = null;
  // Builds the streaming assistant message as an ordered list of content blocks
  // (text interleaved with tool use), mirroring the main chat so the shared
  // message-list renders tool cards in the preview. `activeTextIndex` points at
  // the text block currently accumulating deltas, or -1 when the next text delta
  // should open a fresh block (e.g. right after a tool block).
  private currentMessageBuilder:
    | { id: string; blocks: ContentBlock[]; activeTextIndex: number }
    | null = null;

  // Public readonly signals
  readonly messages = this.messagesSignal.asReadonly();
  readonly isLoading = this.loadingSignal.asReadonly();
  readonly streamingMessageId = this.streamingMessageIdSignal.asReadonly();
  readonly sessionId = this.sessionIdSignal.asReadonly();
  readonly error = this.errorSignal.asReadonly();

  // Computed
  readonly hasMessages = computed(() => this.messagesSignal().length > 0);

  /**
   * Create callbacks for the stream parser.
   * For preview, we only handle basic text streaming.
   */
  private createCallbacks(): StreamParserCallbacks {
    return {
      onMessageStart: () => {
        // Message started - builder already created in sendMessage
      },

      onContentBlockDelta: (data: ContentBlockDeltaEvent) => {
        // Only handle text deltas; tool input arrives whole via onToolUse.
        if (data.text && this.currentMessageBuilder) {
          this.appendText(data.text);
          this.updateCurrentMessage();
        }
      },

      onMessageStop: () => {
        this.loadingSignal.set(false);
        this.streamingMessageIdSignal.set(null);
      },

      onDone: () => {
        this.loadingSignal.set(false);
        this.streamingMessageIdSignal.set(null);
        this.currentMessageBuilder = null;
      },

      onError: (data) => {
        const errorMessage =
          typeof data === 'string'
            ? data
            : (data as { message?: string; error?: string })?.message ||
              (data as { message?: string; error?: string })?.error ||
              'An error occurred';

        this.setErrorMessage(errorMessage);
        this.loadingSignal.set(false);
        this.streamingMessageIdSignal.set(null);
      },

      onStreamError: (data) => {
        this.setErrorMessage(data.message);
        this.loadingSignal.set(false);
        this.streamingMessageIdSignal.set(null);
      },

      onParseError: (message) => {
        console.warn('Preview chat parse error:', message);
      },

      onToolUse: (data: ToolUseEvent) => {
        this.appendToolUse(data);
        this.updateCurrentMessage();
      },

      onToolResult: (data: ToolResultEventData) => {
        this.applyToolResult(data);
        this.updateCurrentMessage();
      },

      // Unused callbacks for preview - just ignore these events
      onContentBlockStart: () => {},
      onContentBlockStop: () => {},
      onToolProgress: () => {},
      onMetadata: () => {},
      onReasoning: () => {},
      onCitation: () => {},
      onQuotaWarning: () => {},
      onQuotaExceeded: () => {},
    };
  }

  /**
   * Send a message in the preview chat.
   * Uses the inference API /invocations endpoint with the preview session ID.
   * Passes current form instructions as system_prompt so the backend uses
   * the live (unsaved) version instead of the persisted one.
   */
  async sendMessage(
    userMessage: string,
    assistantId: string,
    liveInstructions?: string,
    fileUploadIds?: string[],
  ): Promise<void> {
    if (!userMessage.trim() || this.loadingSignal()) {
      return;
    }

    this.errorSignal.set(null);

    // Add user message
    const userMessageId = `msg-${this.sessionIdSignal()}-${this.messagesSignal().length}`;
    const userMsg: Message = {
      id: userMessageId,
      role: 'user',
      content: [{ type: 'text', text: userMessage }],
      createdAt: new Date().toISOString(),
    };
    this.messagesSignal.update((msgs) => [...msgs, userMsg]);

    // Create placeholder for assistant response
    const assistantMessageId = `msg-${this.sessionIdSignal()}-${this.messagesSignal().length}`;
    this.currentMessageBuilder = { id: assistantMessageId, blocks: [], activeTextIndex: -1 };
    const assistantMsg: Message = {
      id: assistantMessageId,
      role: 'assistant',
      content: [{ type: 'text', text: '' }],
      createdAt: new Date().toISOString(),
    };
    this.messagesSignal.update((msgs) => [...msgs, assistantMsg]);
    this.loadingSignal.set(true);
    this.streamingMessageIdSignal.set(assistantMessageId);

    // Abort any previous request
    if (this.abortController) {
      this.abortController.abort();
    }
    this.abortController = new AbortController();

    // Create callbacks once for this stream
    const callbacks = this.createCallbacks();

    try {
      // Phase 6c: route the preview stream through the BFF chat proxy
      // (cookie auth) instead of inference-api directly. Same SSE
      // protocol; the proxy is transparent.
      const appApiUrl = this.config.appApiUrl();
      if (!appApiUrl) {
        throw new Error('App API URL not configured. Please check your configuration.');
      }
      const baseUrl = appApiUrl.endsWith('/') ? appApiUrl.slice(0, -1) : appApiUrl;
      const url = `${baseUrl}/chat/stream`;

      // NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
      // AgentCore Runtime's internal 'assistant_id' field handling (causes 424 error)
      const requestBody: Record<string, unknown> = {
        message: userMessage,
        session_id: this.sessionIdSignal(),
        rag_assistant_id: assistantId,
        system_prompt: liveInstructions || null, // Send live form instructions for preview
        model_id: null, // Use default model
        // Forward the owner's enabled tools so the preview exercises tools the
        // same way a consumer chat does.
        enabled_tools: this.toolService.getEnabledToolIds(),
      };
      if (fileUploadIds && fileUploadIds.length > 0) {
        requestBody['file_upload_ids'] = fileUploadIds;
      }

      // `fetchEventSource` bypasses the HttpClient pipeline, so the
      // csrfInterceptor doesn't run here. Attach X-CSRF-Token by hand.
      const csrfHeaders = this.bffSession.csrfHeaders();

      await this.fetchEventSource(url, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          OAuth2CallbackUrl: `${window.location.origin}/oauth-complete`,
          ...csrfHeaders,
        },
        body: JSON.stringify(requestBody),
        signal: this.abortController.signal,
        onmessage: (msg: EventSourceMessage) => {
          this.handleStreamEvent(msg, callbacks);
        },
        onerror: (err) => {
          console.error('Preview chat SSE error:', err);
          this.handleError(err instanceof Error ? err : new Error(String(err)));
          throw err;
        },
        onclose: () => {
          this.loadingSignal.set(false);
          this.streamingMessageIdSignal.set(null);
          this.currentMessageBuilder = null;
        },
      });
    } catch (error) {
      if ((error as Error)?.name !== 'AbortError') {
        console.error('Preview chat request failed:', error);
        this.handleError(error instanceof Error ? error : new Error(String(error)));
      }
    }
  }

  /**
   * Handle incoming SSE events using the shared parser
   */
  private handleStreamEvent(msg: EventSourceMessage, callbacks: StreamParserCallbacks): void {
    const event = msg.event || 'message';
    let data: unknown = msg.data;

    // Parse JSON data
    if (typeof data === 'string' && data.trim()) {
      try {
        data = JSON.parse(data);
      } catch {
        // Keep as string if not valid JSON
      }
    }

    // Use the shared stream parser
    processStreamEvent(event, data, callbacks);
  }

  /**
   * Append streamed text to the active text block, opening a new one if the
   * previous block was a tool use (so text and tools render in order).
   */
  private appendText(text: string): void {
    const builder = this.currentMessageBuilder;
    if (!builder) {
      return;
    }
    if (builder.activeTextIndex < 0) {
      builder.blocks.push({ type: 'text', text: '' });
      builder.activeTextIndex = builder.blocks.length - 1;
    }
    const block = builder.blocks[builder.activeTextIndex];
    block.text = (block.text ?? '') + text;
  }

  /**
   * Append a tool-use content block. Subsequent text deltas start a fresh text
   * block so the tool card renders between the surrounding prose.
   */
  private appendToolUse(data: ToolUseEvent): void {
    const builder = this.currentMessageBuilder;
    if (!builder) {
      return;
    }
    const toolUse = data.tool_use;
    let input: Record<string, unknown> = {};
    try {
      input = toolUse.input ? JSON.parse(toolUse.input) : {};
    } catch {
      // Tool input JSON may be malformed mid-stream; fall back to empty object.
      input = {};
    }
    const toolUseData: ToolUseData = {
      toolUseId: toolUse.tool_use_id,
      name: toolUse.name,
      input,
      status: 'pending',
    };
    builder.blocks.push({ type: 'toolUse', toolUse: toolUseData });
    builder.activeTextIndex = -1;
  }

  /**
   * Attach a tool result to its matching tool-use block. Replaces the block
   * (new object references) so OnPush message rendering picks up the change.
   */
  private applyToolResult(data: ToolResultEventData): void {
    const builder = this.currentMessageBuilder;
    if (!builder) {
      return;
    }
    const result = data.tool_result;
    const index = builder.blocks.findIndex(
      (block) =>
        block.type === 'toolUse' &&
        (block.toolUse as ToolUseData | undefined)?.toolUseId === result.toolUseId,
    );
    if (index < 0) {
      return;
    }
    const existing = builder.blocks[index].toolUse as ToolUseData;
    const status = result.status === 'error' ? 'error' : 'success';
    builder.blocks[index] = {
      type: 'toolUse',
      toolUse: {
        ...existing,
        result: {
          content: (result.content ?? []) as unknown as ToolResultContent[],
          status,
        },
        status: status === 'error' ? 'error' : 'complete',
      },
    };
  }

  /**
   * Update the current assistant message in the messages array. Rebuilds the
   * content blocks with fresh object references so OnPush change detection in
   * the shared message-list re-renders streaming text and tool cards.
   */
  private updateCurrentMessage(): void {
    if (!this.currentMessageBuilder) {
      return;
    }

    const { id, blocks } = this.currentMessageBuilder;
    const content: ContentBlock[] =
      blocks.length > 0 ? blocks.map((block) => ({ ...block })) : [{ type: 'text', text: '' }];
    this.messagesSignal.update((msgs) => {
      const index = msgs.findIndex((m) => m.id === id);
      if (index >= 0) {
        const updated = [...msgs];
        updated[index] = {
          ...updated[index],
          content,
        };
        return updated;
      }
      return msgs;
    });
  }

  /**
   * Replace the in-flight assistant message with an error notice.
   */
  private setErrorMessage(message: string): void {
    const builder = this.currentMessageBuilder;
    if (!builder) {
      return;
    }
    builder.blocks = [{ type: 'text', text: `Error: ${message}` }];
    builder.activeTextIndex = 0;
    this.updateCurrentMessage();
  }

  /**
   * Handle errors during streaming
   */
  private handleError(error: Error): void {
    this.errorSignal.set(error.message);
    this.loadingSignal.set(false);
    this.streamingMessageIdSignal.set(null);

    if (this.currentMessageBuilder) {
      this.setErrorMessage(error.message);
      this.currentMessageBuilder = null;
    }
  }

  /**
   * Cancel the current request
   */
  cancelRequest(): void {
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
    this.loadingSignal.set(false);
    this.streamingMessageIdSignal.set(null);
    this.currentMessageBuilder = null;
  }

  /**
   * Clear all messages and reset state
   */
  clearMessages(): void {
    this.messagesSignal.set([]);
    this.currentMessageBuilder = null;
    this.errorSignal.set(null);
    this.cancelRequest();
  }

  /**
   * Reset the preview chat with a new session ID
   */
  reset(): void {
    this.clearMessages();
    this.sessionIdSignal.set(`${PREVIEW_SESSION_PREFIX}${uuidv4()}`);
  }
}
