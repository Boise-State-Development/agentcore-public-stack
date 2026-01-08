import { Injectable, inject } from '@angular/core';
import { AuthService } from '../../auth/auth.service';
import { environment } from '../../../environments/environment';
import { fetchEventSource, EventSourceMessage } from '@microsoft/fetch-event-source';

export interface TestChatMessage {
  role: 'user' | 'assistant';
  content: string;
  id: string;
}

export interface TestChatRequest {
  message: string;
  session_id?: string;
}

@Injectable({
  providedIn: 'root'
})
export class TestChatService {
  private authService = inject(AuthService);

  /**
   * Get bearer token for streaming responses (with refresh if needed)
   */
  private async getBearerTokenForStreamingResponse(): Promise<string> {
    // Check if token is expired and refresh if needed
    if (this.authService.isTokenExpired()) {
      try {
        await this.authService.refreshAccessToken();
      } catch (error) {
        console.error('Failed to refresh token:', error);
        // Continue with existing token - let the server handle auth errors
      }
    }

    const token = this.authService.getAccessToken();
    if (!token) {
      throw new Error('No access token available');
    }

    return token;
  }

  /**
   * Send a test chat message to the assistant RAG endpoint
   * Returns an async generator that yields parsed SSE events
   */
  async *sendTestChatMessage(
    assistantId: string,
    message: string,
    sessionId?: string,
    onEvent?: (event: string, data: any) => void
  ): AsyncGenerator<{ event: string; data: any }, void, unknown> {
    const token = await this.getBearerTokenForStreamingResponse();
    const url = `${environment.appApiUrl}/assistants/${assistantId}/test-chat`;

    const requestBody: TestChatRequest = {
      message,
      session_id: sessionId
    };

    await fetchEventSource(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
        'Accept': 'text/event-stream'
      },
      body: JSON.stringify(requestBody),
      onmessage: (msg: EventSourceMessage) => {
        // Parse the data if it's a string
        let parsedData = msg.data;
        if (typeof msg.data === 'string') {
          try {
            parsedData = JSON.parse(msg.data);
          } catch (e) {
            console.warn('Failed to parse SSE data:', msg.data);
            parsedData = msg.data;
          }
        }

        // Yield the event for the caller to handle
        const event = {
          event: msg.event || 'message',
          data: parsedData
        };

        // Call optional callback
        if (onEvent) {
          onEvent(event.event, event.data);
        }

        // Note: We can't actually yield from this callback in a generator
        // So we'll use the callback pattern instead
      },
      onerror: (err) => {
        console.error('Test chat SSE error:', err);
        throw err;
      }
    });
  }

  /**
   * Send a test chat message and handle events via callbacks
   * This is a simpler API that uses callbacks instead of async generators
   */
  async sendTestChatMessageWithCallbacks(
    assistantId: string,
    message: string,
    callbacks: {
      onEvent: (event: string, data: any) => void;
      onError?: (error: Error) => void;
      onClose?: () => void;
    },
    sessionId?: string
  ): Promise<void> {
    const token = await this.getBearerTokenForStreamingResponse();
    const url = `${environment.appApiUrl}/assistants/${assistantId}/test-chat`;

    const requestBody: TestChatRequest = {
      message,
      session_id: sessionId
    };

    try {
      await fetchEventSource(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
          'Accept': 'text/event-stream'
        },
        body: JSON.stringify(requestBody),
        onmessage: (msg: EventSourceMessage) => {
          // Parse the data if it's a string
          let parsedData = msg.data;
          if (typeof msg.data === 'string') {
            try {
              parsedData = JSON.parse(msg.data);
            } catch (e) {
              console.warn('Failed to parse SSE data:', msg.data);
              parsedData = msg.data;
            }
          }

          // Call the event callback
          callbacks.onEvent(msg.event || 'message', parsedData);
        },
        onerror: (err) => {
          console.error('Test chat SSE error:', err);
          if (callbacks.onError) {
            callbacks.onError(err);
          }
          throw err;
        },
        onclose: () => {
          if (callbacks.onClose) {
            callbacks.onClose();
          }
        }
      });
    } catch (error) {
      if (callbacks.onError) {
        callbacks.onError(error instanceof Error ? error : new Error(String(error)));
      }
    }
  }
}

