import { Injectable, Signal, computed, inject, signal, untracked } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { ChatHttpService } from '../../session/services/chat/chat-http.service';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import { StreamParserService } from '../../session/services/chat/stream-parser.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { Message } from '../../session/services/models/message.model';
import { PREVIEW_SESSION_PREFIX } from '../constants/session.constants';

/**
 * Owns one embedded preview's session id and projects the shared chat stack's
 * per-session state for it.
 *
 * This is **glue, not a second chat implementation** — the distinction that
 * matters here, because the thing it replaces was the second implementation.
 * `PreviewChatService` re-implemented the SSE consumer and handled 9 of the
 * ~27 events the shared parser dispatches; since every callback in that parser
 * is optional, the ones it never implemented — `tool_approval_required`,
 * `oauth_required`, `model_retry`, `artifact`, `ui_resource`, … — were dropped
 * with no error. A tool call that paused for approval was therefore never
 * surfaced, never answered, and never dispatched: the pane sat on "Thinking"
 * while the backing service was never called at all.
 *
 * So nothing below parses, streams, or interprets anything. Every method
 * delegates to the same services the main chat uses, and every signal reads
 * that stack's state keyed by this pane's `preview-` session id. The chat stack
 * has been session-keyed all along (`getMessagesForSession`, `stateFor`, the
 * parser's per-session state), which is why the fork's one real benefit —
 * isolation from the user's real conversations — needed no fork at all.
 *
 * Provide it at the **component** level: one instance per pane, so two previews
 * open at once (say, two rows of the review queue) never share a session id.
 */
@Injectable()
export class PreviewSessionService {
  private readonly chatRequest = inject(ChatRequestService);
  private readonly chatHttp = inject(ChatHttpService);
  private readonly chatState = inject(ChatStateService);
  private readonly streamParser = inject(StreamParserService);
  private readonly messageMap = inject(MessageMapService);

  private readonly sessionIdSignal = signal<string>(newPreviewSessionId());

  /**
   * The message-map's signal for the current session. Held indirectly because
   * `getMessagesForSession` *writes* the map when a session is first seen, and
   * a write inside a computed is an Angular error — so it is called from event
   * handlers only, and the returned signal is stored. Mirrors `session.page.ts`.
   */
  private readonly messagesRef = signal<Signal<Message[]>>(signal([]));

  readonly sessionId = this.sessionIdSignal.asReadonly();
  readonly messages = computed(() => this.messagesRef()());
  readonly hasMessages = computed(() => this.messages().length > 0);

  /**
   * Read per-session rather than through `ChatStateService`'s viewed-session
   * facades: those drive the main composer, and a preview must never claim them.
   */
  readonly isLoading = computed(() => this.chatState.isSessionLoading(this.sessionId()));

  readonly streamingMessageId = computed(() =>
    this.streamParser.streamingMessageIdFor(this.sessionId())(),
  );

  /**
   * Parse-level stream error for this session, for panes that render an inline
   * strip. Transport and server errors (401/403/409/5xx) are surfaced by
   * `ChatHttpService` through `ErrorService` instead, and a conversational
   * error arrives as a `stream_error` the message list renders in place — so
   * this is deliberately narrow rather than a second error channel.
   */
  readonly error = computed(() => this.streamParser.errorFor(this.sessionId())());

  constructor() {
    this.bind(this.sessionIdSignal());
  }

  /**
   * ⚠️ Every method below reads the session id through `untracked`.
   *
   * These are commands, not derivations, and callers invoke them from reactive
   * contexts — `AgentPreviewComponent` and `ReviewTestDriveComponent` both call
   * `reset()` from an `effect` that watches the agent id. `reset()` reads the
   * current session id and then writes a new one; tracked, that read-then-write
   * on the same signal makes the effect re-trigger itself forever. It does not
   * fail loudly — it pegs the main thread, so the page paints its skeleton, the
   * HTTP responses land but are never processed, and the whole tab looks like
   * an API that never resolved.
   *
   * The service this replaced avoided the cycle only by accident: its cancel
   * path read a plain `abortController` field rather than the session signal.
   */

  /** Send one preview turn. Resolves when the stream finishes. */
  async send(
    agentId: string,
    message: string,
    opts?: { fileUploadIds?: string[]; reviewPreview?: boolean },
  ): Promise<void> {
    const sessionId = untracked(this.sessionId);
    await this.chatRequest.submitPreviewRequest({
      sessionId,
      agentId,
      message,
      fileUploadIds: opts?.fileUploadIds,
      reviewPreview: opts?.reviewPreview,
    });
  }

  /** Stop the in-flight turn (Stop button). */
  cancel(): void {
    this.chatHttp.cancelChatRequest(untracked(this.sessionId));
  }

  /**
   * Clear the transcript and start a fresh preview session.
   *
   * Minting a NEW session id is the point, not a side effect. Aborting the SSE
   * does not stop the agent server-side — a client abort doesn't propagate
   * through the AgentCore Runtime data plane — so the old run may still be
   * alive. Reusing the id meant the next message collided with it, which is
   * what surfaced as "Agent is already processing a request." A new id cannot
   * collide, and the abandoned run has nothing to corrupt: preview sessions
   * persist nothing.
   */
  reset(): void {
    untracked(() => {
      const previous = this.sessionIdSignal();
      this.cancel();
      this.messageMap.clearSession(previous);

      const next = newPreviewSessionId();
      this.sessionIdSignal.set(next);
      this.bind(next);
    });
  }

  private bind(sessionId: string): void {
    this.messagesRef.set(this.messageMap.getMessagesForSession(sessionId));
  }
}

/**
 * A session id the backend recognises as a preview: it skips persistence,
 * session metadata, and the single-flight lease for these.
 */
function newPreviewSessionId(): string {
  return `${PREVIEW_SESSION_PREFIX}${uuidv4()}`;
}
