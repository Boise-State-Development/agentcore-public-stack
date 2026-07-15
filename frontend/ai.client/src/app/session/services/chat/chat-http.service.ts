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
/**
 * Thrown by the SSE `onopen` when the BFF returned 409 — the inference-api
 * single-flight guard rejected this turn because another turn for the same
 * session is still streaming server-side (a second tab/device, a transport
 * retry, or a Stop whose server turn hasn't ended, since a client abort does
 * not propagate through the AgentCore Runtime). Not a failure: `onerror`
 * surfaces it as a gentle notice rather than an error toast.
 */
class AlreadyStreamingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AlreadyStreamingError';
  }
}

/**
 * Best-effort human message for a 409 from `/chat/stream`. The app-api proxy
 * relays the inference-api body verbatim inside its own `detail`, so the
 * payload can be double-encoded (`{"detail":"{\"detail\":\"…\"}"}`) — unwrap one
 * nested layer, and fall back to a fixed message if the body is unreadable.
 */
async function parseConflictMessage(response: Response): Promise<string> {
  const fallback =
    'This conversation is still generating a response. Wait for it to finish before sending another message.';
  try {
    const data = await response.json();
    let detail: unknown = data?.detail ?? data?.error?.message ?? data?.message;
    if (typeof detail === 'string' && detail.trim().startsWith('{')) {
      try {
        detail = (JSON.parse(detail) as { detail?: unknown })?.detail ?? detail;
      } catch {
        // Not nested JSON after all — keep the string as-is.
      }
    }
    return typeof detail === 'string' && detail.trim() ? detail : fallback;
  } catch {
    return fallback;
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
    const sessionId = requestObject.session_id as string;

    // Fresh controller per request, keyed by session. Creating it aborts any
    // in-flight stream for the SAME session (double-submit guard) without
    // touching other sessions' streams.
    const abortController = this.chatStateService.createAbortController(sessionId);

    // Capture this stream's identity. The caller reset the parser for this
    // session (startStreaming / beginContinuationStreaming) just before this
    // call, so the current stream ID is ours. Events and lifecycle callbacks
    // check it before touching state — if a newer stream has since reset the
    // session, this stream is superseded and must leave everything alone.
    const streamId = this.streamParserService.getCurrentStreamId(sessionId);
    const isCurrentStream = () =>
      this.streamParserService.getCurrentStreamId(sessionId) === streamId;
    // Sole owner of this stream's teardown (loading flag + streaming state).
    // The guard makes it a no-op once a newer stream owns the session, so a
    // superseded stream's late close/error can never tear down its
    // replacement. Callers must NOT duplicate this cleanup in their catch
    // blocks — an unguarded duplicate reintroduces exactly that race.
    const finalizeStream = () => {
      if (!isCurrentStream()) return;
      this.messageMapService.endStreaming(sessionId);
      this.chatStateService.setChatLoading(sessionId, false);
    };

    try {
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

      return await fetchEventSource(`${baseUrl}/chat/stream`, {
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
        // Keep the stream open when the tab is backgrounded. With the library
        // default (`openWhenHidden: false`) fetch-event-source aborts the
        // connection on `visibilitychange` to hidden and REOPENS it — issuing
        // a brand-new `POST /invocations` for the SAME turn — when the tab
        // becomes visible again. That reopen happens inside the library, reusing
        // this request and bypassing our per-session double-submit + streamId
        // supersession guards, so nothing here catches it. Because a client
        // abort does NOT propagate through the AgentCore Runtime data plane, the
        // original backend agent keeps running; the reopened one runs the same
        // turn concurrently, and both persist tool-use/tool-result events to the
        // same AgentCore Memory session — corrupting history (duplicate /
        // interleaved toolResult turns) and bricking the conversation with a
        // Bedrock "toolResult blocks exceed toolUse blocks" ValidationException.
        // Keeping the single stream alive across tab switches is also correct for
        // long agentic turns. See the restore-time repair in
        // TurnBasedSessionManager for the server-side safety net.
        openWhenHidden: true,
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
          } else if (response.status === 409) {
            // Single-flight guard: another turn for this session is still
            // streaming server-side. This is a benign rejection, not a
            // failure — surface a gentle notice (handled in `onerror`) and
            // don't tear down as fatal/retriable.
            throw new AlreadyStreamingError(await parseConflictMessage(response));
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
          this.streamParserService.parseEventSourceMessage(
            sessionId,
            msg.event,
            parsedData,
            streamId,
          );
        },
        onclose: () => {
          finalizeStream();

          // Fallback only: the title normally arrives mid-stream as a
          // `session_title` SSE event, whose handler (applyServerTitle)
          // removes the session from newSessionIds — making this a no-op.
          // It still fires when the stream outran title generation (fast
          // response) or the event was lost, fetching the title Nova Micro
          // wrote to session metadata concurrently with the stream.
          if (this.sessionService.isNewSession(requestObject.session_id)) {
            this.refreshTitleFromServer(requestObject.session_id);
          }
        },
        onerror: (err) => {
          finalizeStream();

          // 401 already triggered the redirect — skip the toast so it
          // doesn't flash before the page tears down.
          if (err instanceof UnauthorizedError) {
            throw err;
          }

          // 409 single-flight rejection is expected, not an error: the prior
          // response is still generating. Show a soft, dismissible notice with
          // the server's explanation rather than a "Chat Request Failed" toast.
          if (err instanceof AlreadyStreamingError) {
            this.errorService.addError('Already responding', err.message, undefined, undefined);
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
    } catch (error) {
      // Guarded teardown for failures fetchEventSource surfaces as a
      // rejection (and for the pre-flight config throw above). Idempotent
      // with the onerror path; no-op if a newer stream owns the session.
      finalizeStream();
      throw error;
    }
  }

  /**
   * Stop one session's in-flight stream. Only that session is affected —
   * other conversations streaming concurrently keep going. The aborted
   * fetch resolves silently (fetch-event-source calls neither onclose nor
   * onerror on abort), so the streaming teardown happens here.
   */
  cancelChatRequest(sessionId: string): void {
    // Authoritative intent signal: the transport can't distinguish a Stop
    // click from a dropped socket, so we tell the backend explicitly this
    // was deliberate BEFORE aborting (so the request is queued while the
    // connection is still alive). `keepalive` survives page teardown and,
    // unlike `navigator.sendBeacon`, can carry the `X-CSRF-Token` header the
    // app-api CSRFMiddleware requires on unsafe cookie-authenticated methods.
    // Best-effort / fire-and-forget — the user's Stop must never block on it.
    this.signalUserStopped(sessionId);
    // Reflect the interruption locally right away so the reload chip also
    // shows within the same session, without waiting for a refresh. The
    // backend marker (raced with the cancellation backstop) is the
    // refresh-survival source of truth.
    this.chatStateService.setLastTurnInterrupted(sessionId, true, 'user_stopped');

    this.chatStateService.abortRequest(sessionId);
    this.messageMapService.endStreaming(sessionId);
    this.chatStateService.setChatLoading(sessionId, false);

    // Aborting the fetch cut the socket before the stream's terminal
    // `metadata` SSE (usage / cost / context) could arrive, so the session
    // cost badge would otherwise stay stale for this turn. The backend
    // persists that metadata during its interruption teardown instead; pull
    // the freshly-bumped session aggregates to light the badge up live. The
    // write races our abort, so this fetch is delayed + retried. (The
    // per-message token/cost badges hydrate from the same persisted row on
    // the next message reload.)
    setTimeout(() => void this.refreshAggregatesAfterStop(sessionId), 900);
  }

  /**
   * Re-seed a session's cost/context badge after a Stop, once the backend's
   * interruption teardown has persisted the partial turn's metadata (which
   * bumps the denormalized session aggregates). Retries once because that
   * write races the client abort; best-effort — the next navigation's
   * `seedSessionAggregates` is the durable backstop.
   */
  private async refreshAggregatesAfterStop(sessionId: string, retried = false): Promise<void> {
    try {
      const metadata = await this.sessionService.getSessionMetadata(sessionId);
      const hasAggregates =
        (metadata.totalCost ?? 0) > 0 ||
        (metadata.lastContextTokens ?? 0) > 0 ||
        (metadata.contextWindow ?? 0) > 0;
      if (hasAggregates) {
        this.chatStateService.seedSessionAggregates(sessionId, {
          totalCost: metadata.totalCost,
          lastContextTokens: metadata.lastContextTokens,
          contextWindow: metadata.contextWindow,
        });
        return;
      }
      if (!retried) {
        setTimeout(() => void this.refreshAggregatesAfterStop(sessionId, true), 1500);
      }
    } catch (error) {
      console.error('Failed to refresh session cost after stop:', error);
    }
  }

  /** Fire-and-forget POST to app-api recording deliberate stop intent. */
  private signalUserStopped(sessionId: string): void {
    try {
      const appApiUrl = this.config.appApiUrl();
      if (!appApiUrl) return;
      const baseUrl = appApiUrl.endsWith('/') ? appApiUrl.slice(0, -1) : appApiUrl;
      void fetch(`${baseUrl}/sessions/${encodeURIComponent(sessionId)}/interrupt`, {
        method: 'POST',
        keepalive: true,
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          ...this.bffSession.csrfHeaders(),
        },
        body: JSON.stringify({ reason: 'user_stopped' }),
      }).catch(() => {
        // Best-effort: a failed signal degrades to the server-side
        // connection_lost backstop (or nothing if the turn completed).
      });
    } catch {
      // Never let intent signalling interfere with the Stop action.
    }
  }

  /**
   * Fallback pull of the server-generated title (sidebar cache + header).
   *
   * Title generation runs concurrently with the agent stream on the backend
   * and is normally PUSHED mid-stream as a `session_title` SSE event; this
   * fetch only runs when the stream closed while the session still looked
   * new (see onclose). On a "New Conversation" placeholder — generation
   * still in flight or failed — we retry once after a short delay before
   * giving up.
   */
  private async refreshTitleFromServer(sessionId: string, retried = false): Promise<void> {
    try {
      const metadata = await this.sessionService.getSessionMetadata(sessionId);
      if (metadata.title && metadata.title !== 'New Conversation') {
        this.sessionService.applyServerTitle(sessionId, metadata.title);
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
