import { DOCUMENT, isPlatformBrowser } from '@angular/common';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, PLATFORM_ID, inject } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';

/**
 * Starts a conversation's AgentCore Runtime session before its next send.
 *
 * The Runtime starts a conversation's microVM only when an invocation arrives
 * for it, so the first send used to pay that start (session creation and a V2
 * snapshot restore, about 1.5–2 s). This service asks app-api
 * (`POST /chat/prewarm`) to send a no-op warm invocation pinned to the same
 * microVM the next turn will use, so the start overlaps the user reading and
 * typing. See docs/specs/agentcore-runtime-v2.md §5a.
 *
 * Two triggers, matched to how strong each page's intent to send is:
 * - **A new conversation warms on page load** ({@link warmNewConversation} /
 *   {@link warm}). Opening it is almost always intent to send, and it is where
 *   paste-then-send and prompt-starter sends happen, which a keystroke trigger
 *   would miss or warm too late. Only while the tab is visible; a background
 *   tab warms when the user first looks at it, and again on return after
 *   {@link REWARM_AFTER_MS}.
 * - **An existing conversation warms on the composer's first input**
 *   ({@link armOnTyping} then {@link noteTyping}). Opening one is often just
 *   reading, and a reply takes long enough to type that the warm still
 *   finishes first.
 *
 * Common to both:
 * - At most once per conversation per {@link REWARM_AFTER_MS}, inside the
 *   Runtime's 900 s idle timeout; a real turn counts ({@link noteTurn}).
 *   app-api applies the same window and a per-user rate limit.
 * - Fire-and-forget, errors swallowed without a toast. A send never waits for
 *   a warm call; one that lands mid-warm shares the microVM while it starts.
 * - A new conversation's id is minted here and handed to its first send
 *   through {@link claimNewConversationId}, so the turn lands on the warmed
 *   microVM. While the flag is off nothing is minted or sent.
 */
@Injectable({ providedIn: 'root' })
export class SessionPrewarmService {
  /** Re-warm the same conversation no sooner than this (app-api uses the same 10 minutes). */
  static readonly REWARM_AFTER_MS = 10 * 60 * 1000;

  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly features = inject(FEATURES);
  private readonly document = inject(DOCUMENT);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private readonly lastWarmedAt = new Map<string, number>();
  private pendingNewConversationId: string | null = null;
  /** The new conversation the visible page shows; re-warmed when the tab comes back. */
  private currentId: string | null = null;
  /** The existing conversation the page shows, warmed on the composer's first input. */
  private armedId: string | null = null;
  private listening = false;

  /** Whether this build warms sessions at all. */
  get enabled(): boolean {
    return this.features.sessionPrewarm && this.isBrowser;
  }

  /**
   * Warm the conversation a new-conversation page will start, minting its id.
   * The same id is returned until a send claims it.
   */
  warmNewConversation(): void {
    if (!this.enabled) {
      return;
    }
    this.pendingNewConversationId ??= uuidv4();
    this.warm(this.pendingNewConversationId);
  }

  /**
   * The id a new conversation should start with, if one was warmed; clears it.
   * Null while the flag is off or before anything was warmed, and the caller
   * mints its own.
   */
  claimNewConversationId(): string | null {
    const id = this.pendingNewConversationId;
    this.pendingNewConversationId = null;
    return id;
  }

  /** Warm a new (staged) conversation now: the page-load trigger. */
  warm(sessionId: string | null | undefined): void {
    if (!this.enabled || !sessionId) {
      return;
    }
    this.armedId = null;
    this.currentId = sessionId;
    this.listenForVisibility();
    if (this.document.visibilityState !== 'visible') {
      return; // warmed by the visibility listener when the user looks at it
    }
    this.send(sessionId);
  }

  /**
   * Arm an existing conversation to warm on the composer's first input,
   * instead of on load: opening one is often just reading.
   */
  armOnTyping(sessionId: string | null | undefined): void {
    if (!this.enabled || !sessionId) {
      return;
    }
    this.currentId = null;
    this.armedId = sessionId;
  }

  /**
   * The user typed or pasted into the composer. Warms the armed conversation,
   * once per window; a no-op on any page that armed nothing, so composers
   * outside the conversation page never warm.
   */
  noteTyping(): void {
    if (this.enabled && this.armedId) {
      this.send(this.armedId);
    }
  }

  /** A real turn started on this conversation, which starts its microVM too. */
  noteTurn(sessionId: string | null | undefined): void {
    if (sessionId) {
      this.lastWarmedAt.set(sessionId, Date.now());
    }
  }

  /** Forget the page's conversation, so neither returning nor typing warms it. */
  clearCurrent(): void {
    this.currentId = null;
    this.armedId = null;
  }

  private send(sessionId: string): void {
    const now = Date.now();
    const last = this.lastWarmedAt.get(sessionId);
    if (last !== undefined && now - last < SessionPrewarmService.REWARM_AFTER_MS) {
      return;
    }
    this.lastWarmedAt.set(sessionId, now);
    this.http
      .post(
        `${this.config.appApiUrl()}/chat/prewarm`,
        { session_id: sessionId },
        { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) },
      )
      .subscribe({
        // A failed warm only means the first send starts the microVM itself.
        error: () => undefined,
      });
  }

  private listenForVisibility(): void {
    if (this.listening) {
      return;
    }
    this.listening = true;
    this.document.addEventListener('visibilitychange', () => {
      if (this.document.visibilityState === 'visible' && this.currentId) {
        this.send(this.currentId);
      }
    });
  }
}
