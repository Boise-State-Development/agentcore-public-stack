import { DOCUMENT, isPlatformBrowser } from '@angular/common';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, PLATFORM_ID, inject } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';

/**
 * Starts a conversation's AgentCore Runtime session before its first send.
 *
 * The Runtime starts a conversation's microVM only when an invocation arrives
 * for it, so the first send used to pay that start (a V2 snapshot restore plus
 * first-turn setup). Opening a conversation now asks app-api
 * (`POST /chat/prewarm`) to send a no-op warm invocation pinned to the same
 * microVM the first turn will use, so the start overlaps the user reading and
 * typing. See docs/specs/agentcore-runtime-v2.md §5a.
 *
 * - **When:** as the conversation page loads, which is when the composer takes
 *   focus, but only while the tab is visible. A tab opened in the background
 *   warms when the user switches to it, not before.
 * - **How often:** at most once per conversation per {@link REWARM_AFTER_MS},
 *   inside the Runtime's 900 s idle timeout; returning to a tab after that
 *   warms it again. app-api applies the same window and a per-user rate limit.
 * - **Never in the way:** fire-and-forget, errors swallowed without a toast.
 *   A send never waits for a warm call; one that lands mid-warm just shares the
 *   microVM while it starts.
 * - **A new conversation:** the warmed id is minted here and handed to the
 *   first send through {@link claimNewConversationId}, so the turn lands on the
 *   microVM that was warmed. While the flag is off nothing is minted and the
 *   send mints its own id, exactly as before.
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
  /** The conversation the visible page shows; re-warmed when the tab comes back. */
  private currentId: string | null = null;
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

  /** Warm an existing (or staged) conversation the user just opened. */
  warm(sessionId: string | null | undefined): void {
    if (!this.enabled || !sessionId) {
      return;
    }
    this.currentId = sessionId;
    this.listenForVisibility();
    if (this.document.visibilityState !== 'visible') {
      return; // warmed by the visibility listener when the user looks at it
    }
    this.send(sessionId);
  }

  /** Forget the page's conversation, so returning to the tab warms nothing. */
  clearCurrent(): void {
    this.currentId = null;
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
