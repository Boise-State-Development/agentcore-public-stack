import { Injectable, inject, signal, computed } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { fetchEventSource, EventSourceMessage } from '@microsoft/fetch-event-source';
import { AuthService } from '../../../auth/auth.service';
import { ConfigService } from '../../../services/config.service';
import { Message } from '../../../session/services/models/message.model';
import { PREVIEW_SESSION_PREFIX } from '../../../shared/constants/session.constants';
import {
  processStreamEvent,
  type StreamParserCallbacks,
  type ContentBlockDeltaEvent,
} from '../../../shared/utils/stream-parser';

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
 * For preview, we only need basic text streaming - tool use, citations, and other advanced
 * features are not critical for testing assistant instructions.
 */
@Injectable()
export class PreviewChatService {
  private authService = inject(AuthService);
  private config = inject(ConfigService);

  // Local state signals (isolated from global ChatStateService)
  private readonly messagesSignal = signal<Message[]>([]);
  private readonly loadingSignal = signal<boolean>(false);
  private readonly streamingMessageIdSignal = signal<string | null>(null);
  private readonly sessionIdSignal = signal<string>(`${PREVIEW_SESSION_PREFIX}${uuidv4()}`);
  private readonly errorSignal = signal<string | null>(null);

  // Abort controller for cancellation
  private abortController: AbortController | null = null;
  private currentMessageBuilder: { id: string; content: string } | null = null;

  // Public readonly signals
  readonly messages = this.messagesSignal.asReadonly();
  readonly isLoading = this.loadingSignal.asReadonly();
  readonly streamingMessageId = this.streamingMessageIdSignal.asReadonly();
  readonly sessionId = this.sessionIdSignal.asReadonly();
  readonly error = this.errorSignal.asReadonly();

  // Computed
  readonly hasMessages = computed(() => this.messagesSignal().length > 0);

  /**
   * Get bearer token for streaming responses (with refresh if needed)
   */
  private async getBearerTokenForStreamingResponse(): Promise<string> {
    if (this.authService.isTokenExpired()) {
      try {
        await this.authService.refreshAccessToken();
      } catch (error) {
        console.error('Failed to refresh token:', error);
      }
    }

    const token = this.authService.getAccessToken();
    if (!token) {
      throw new Error('No access token available');
    }

    return token;
  }

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
        // Only handle text deltas
        if (data.text && this.currentMessageBuilder) {
          this.currentMessageBuilder.content += data.text;
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

        if (this.currentMessageBuilder) {
          this.currentMessageBuilder.content = `Error: ${errorMessage}`;
          this.updateCurrentMessage();
        }
        this.loadingSignal.set(false);
        this.streamingMessageIdSignal.set(null);
      },

      onStreamError: (data) => {
        if (this.currentMessageBuilder) {
          this.currentMessageBuilder.content = `Error: ${data.message}`;
          this.updateCurrentMessage();
        }
        this.loadingSignal.set(false);
        this.streamingMessageIdSignal.set(null);
      },

      onParseError: (message) => {
        console.warn('Preview chat parse error:', message);
      },

      // Unused callbacks for preview - just ignore these events
      onContentBlockStart: () => {},
      onContentBlockStop: () => {},
      onToolUse: () => {},
      onToolResult: () => {},
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
  async sendMessage(userMessage: string, assistantId: string, liveInstructions?: string): Promise<void> {
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
      created_at: new Date().toISOString(),
    };
    this.messagesSignal.update((msgs) => [...msgs, userMsg]);

    // Create placeholder for assistant response
    const assistantMessageId = `msg-${this.sessionIdSignal()}-${this.messagesSignal().length}`;
    this.currentMessageBuilder = { id: assistantMessageId, content: '' };
    const assistantMsg: Message = {
      id: assistantMessageId,
      role: 'assistant',
      content: [{ type: 'text', text: '' }],
      created_at: new Date().toISOString(),
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
      const token = await this.getBearerTokenForStreamingResponse();

      // Single runtime endpoint from configuration
      const runtimeEndpointUrl = this.config.inferenceApiUrl();
      if (!runtimeEndpointUrl) {
        throw new Error('Inference API URL not configured. Please check your configuration.');
      }
      const url = `${runtimeEndpointUrl}/invocations?qualifier=DEFAULT`;

      // NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
      // AgentCore Runtime's internal 'assistant_id' field handling (causes 424 error)
      const requestBody = {
        message: userMessage,
        session_id: this.sessionIdSignal(),
        rag_assistant_id: assistantId,
        system_prompt: liveInstructions || null, // Send live form instructions for preview
        model_id: null, // Use default model
        enabled_tools: [], // No tools in preview
      };

      await fetchEventSource(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
          Accept: 'text/event-stream',
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
   * Update the current assistant message in the messages array
   */
  private updateCurrentMessage(): void {
    if (!this.currentMessageBuilder) {
      return;
    }

    const { id, content } = this.currentMessageBuilder;
    this.messagesSignal.update((msgs) => {
      const index = msgs.findIndex((m) => m.id === id);
      if (index >= 0) {
        const updated = [...msgs];
        updated[index] = {
          ...updated[index],
          content: [{ type: 'text', text: content }],
        };
        return updated;
      }
      return msgs;
    });
  }

  /**
   * Handle errors during streaming
   */
  private handleError(error: Error): void {
    this.errorSignal.set(error.message);
    this.loadingSignal.set(false);
    this.streamingMessageIdSignal.set(null);

    if (this.currentMessageBuilder) {
      this.currentMessageBuilder.content = `Error: ${error.message}`;
      this.updateCurrentMessage();
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
