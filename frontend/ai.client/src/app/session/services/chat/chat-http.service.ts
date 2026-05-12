import { inject, Injectable } from '@angular/core';
import { EventSourceMessage, fetchEventSource } from '@microsoft/fetch-event-source';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { ConfigService } from '../../../services/config.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { firstValueFrom } from 'rxjs';
import { HttpClient } from '@angular/common/http';
import { SessionService } from '../session/session.service';
import { ErrorService } from '../../../services/error/error.service';

class RetriableError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = 'RetriableError';
  }
}
class FatalError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = 'FatalError';
  }
}
/**
 * Thrown by the SSE `onopen` when the BFF returned 401. The session
 * redirect is already in flight; `onerror` uses this marker to suppress
 * the toast that would otherwise flash before the page tears down.
 */
class UnauthorizedError extends Error {
  constructor() {
    super('Session expired');
    this.name = 'UnauthorizedError';
  }
}

interface GenerateTitleRequest {
  session_id: string;
  input: string;
}

interface GenerateTitleResponse {
  title: string;
  session_id: string;
}

@Injectable({
  providedIn: 'root',
})
export class ChatHttpService {
  private streamParserService = inject(StreamParserService);
  private chatStateService = inject(ChatStateService);
  private messageMapService = inject(MessageMapService);
  private config = inject(ConfigService);
  private http = inject(HttpClient);
  private sessionService = inject(SessionService);
  private bffSession = inject(BffSessionService);
  private errorService = inject(ErrorService);

  async sendChatRequest(requestObject: any): Promise<void> {
    const abortController = this.chatStateService.getAbortController();

    // Phase 6c: stream goes through the app-api BFF proxy at
    // `${appApiUrl}/chat/stream` (cookie auth) instead of hitting
    // inference-api `/invocations` directly with a Bearer token. Same
    // SSE protocol on the wire — the proxy is transparent. Cookies
    // travel because the SPA and `/api/*` are same-origin via
    // CloudFront; for local dev the same origin still works because
    // the SPA is configured to point at `http://localhost:8000` (the
    // app-api directly) and the BFF dual-auth dep accepts Bearer too.
    const appApiUrl = this.config.appApiUrl();
    if (!appApiUrl) {
      throw new FatalError('App API URL not configured. Please check your configuration.');
    }
    const baseUrl = appApiUrl.endsWith('/') ? appApiUrl.slice(0, -1) : appApiUrl;

    // `fetchEventSource` is outside the HttpClient pipeline, so the
    // csrfInterceptor never runs against it — attach the X-CSRF-Token
    // header manually using the same SessionService helper. Returns
    // an empty object before bootstrap or in the Bearer rollback path.
    const csrfHeaders = this.bffSession.csrfHeaders();

    // Capture into a local so `onopen` (a method-shorthand on the config
    // object, not a closure over `this`) can reach the BFF session.
    const bffSession = this.bffSession;

    return fetchEventSource(`${baseUrl}/chat/stream`, {
      method: 'POST',
      // Send the BFF session cookie (`__Host-bff_session`) on cross-origin
      // dev (localhost:4200 → localhost:8000) and on same-origin prod
      // (CloudFront). Browsers attach same-origin cookies regardless;
      // `include` is the explicit form that also works cross-origin
      // when the backend's CORS allows credentials.
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        OAuth2CallbackUrl: `${window.location.origin}/oauth-complete`,
        ...csrfHeaders,
      },
      body: JSON.stringify(requestObject),
      signal: abortController.signal,
      async onopen(response) {
        if (response.ok && response.headers.get('content-type')?.includes('text/event-stream')) {
          return; // everything's good
        } else if (response.status === 401) {
          // BFF session is missing or expired. Bounce to /auth/login —
          // `handleUnauthorized` is idempotent, so a 401 here that races
          // with one from a parallel request only navigates once.
          bffSession.handleUnauthorized();
          throw new UnauthorizedError();
        } else if (response.status === 403) {
          // Handle forbidden (e.g., usage limit exceeded)
          let errorMessage = 'Access forbidden';

          try {
            const errorData = await response.json();
            if (errorData.error) {
              // Structured error from backend
              errorMessage = errorData.error.message || errorMessage;
            } else if (errorData.message) {
              errorMessage = errorData.message;
            }
          } catch {
            // Response not JSON, use default
          }

          throw new FatalError(errorMessage);
        } else if (response.status >= 400 && response.status < 500 && response.status !== 429) {
          // Client-side errors are usually non-retriable
          let errorMessage = `Request failed with status ${response.status}`;

          try {
            const errorData = await response.json();
            if (errorData.error) {
              // Structured error from backend
              errorMessage = errorData.error.message || errorMessage;
            } else if (errorData.message) {
              errorMessage = errorData.message;
            }
          } catch {
            // If response is not JSON, try to get text
            try {
              const errorText = await response.text();
              errorMessage = errorText || errorMessage;
            } catch {
              // Ignore if we can't read the response
            }
          }

          throw new FatalError(errorMessage);
        } else {
          // Server errors or unexpected status codes (retriable)
          const errorMessage = `Server error: ${response.status} ${response.statusText}`;
          console.error('RetriableError:', errorMessage);
          throw new RetriableError(errorMessage);
        }
      },
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
        this.streamParserService.parseEventSourceMessage(msg.event, parsedData);
      },
      onclose: () => {
        this.messageMapService.endStreaming();
        this.chatStateService.setChatLoading(false);

        // Title is generated server-side concurrently with the stream
        // (see /invocations). Refresh metadata so the sidebar reflects it.
        if (this.sessionService.isNewSession(requestObject.session_id)) {
          this.refreshTitleFromServer(requestObject.session_id);
        }
      },
      onerror: (err) => {
        this.messageMapService.endStreaming();
        this.chatStateService.setChatLoading(false);

        // 401 already triggered the redirect — skip the toast so it
        // doesn't flash before the page tears down.
        if (err instanceof UnauthorizedError) {
          throw err;
        }

        // Display error message to user using ErrorService
        if (err instanceof FatalError) {
          this.errorService.addError('Chat Request Failed', err.message, undefined, undefined);
        } else if (err instanceof RetriableError) {
          // For retriable errors, show with retry suggestion
          this.errorService.addError(
            'Connection Error',
            'A temporary connection error occurred. The request may be retried automatically.',
            err.message,
          );
        } else {
          // Unknown error type
          this.errorService.handleNetworkError(err instanceof Error ? err.message : String(err));
        }

        throw err;
      },
    });
  }

  cancelChatRequest(): void {
    // First abort the client-side request
    this.chatStateService.abortCurrentRequest();

    this.chatStateService.setChatLoading(false);

    // Cleanup request-conversation mapping when cancelled
    this.chatStateService.resetState();
  }

  /**
   * Pull the server-generated title into the local sidebar cache.
   *
   * Title generation runs concurrently with the agent stream on the backend.
   * Nova Micro typically finishes well before the stream does, but on fast
   * responses we may race past it — so on a "New Conversation" placeholder
   * we retry once after a short delay before giving up.
   */
  private async refreshTitleFromServer(sessionId: string, retried = false): Promise<void> {
    try {
      const metadata = await this.sessionService.getSessionMetadata(sessionId);
      if (metadata.title && metadata.title !== 'New Conversation') {
        this.sessionService.updateSessionTitleInCache(sessionId, metadata.title);
        return;
      }
      if (!retried) {
        setTimeout(() => this.refreshTitleFromServer(sessionId, true), 1500);
      }
    } catch (error) {
      console.error('Failed to refresh session title:', error);
    }
  }

  /**
   * Generates a title for a session based on the user's input.
   *
   * @param sessionId - The session ID
   * @param userInput - The user's input message
   * @returns Promise resolving to GenerateTitleResponse with the generated title
   * @throws Error if the API request fails
   */
  async generateTitle(sessionId: string, userInput: string): Promise<GenerateTitleResponse> {
    const requestBody: GenerateTitleRequest = {
      session_id: sessionId,
      input: userInput,
    };

    try {
      const response = await firstValueFrom(
        this.http.post<GenerateTitleResponse>(
          `${this.config.appApiUrl()}/chat/generate-title`,
          requestBody,
        ),
      );
      return response;
    } catch (error) {
      console.error('Failed to generate title:', error);
      // Use ErrorService for non-critical errors (title generation)
      // Don't show to user as it's a background operation
      throw error;
    }
  }

}
