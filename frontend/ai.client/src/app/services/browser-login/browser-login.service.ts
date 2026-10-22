import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../config.service';

/**
 * A paused turn waiting for the user to sign in to a site the agent cannot
 * reach (`docs/specs/authenticated-web-assessment.md`).
 *
 * Sibling of `UserQuestionRequest` — same tool-raised interrupt, same resume
 * contract — with one structural difference: the prompt carries **no URL**.
 * A Live View URL is SigV4 query-signed and lives at most 300 seconds, so it
 * is fetched from `/sessions/{id}/browser/live-view` when the viewer opens and
 * re-fetched before it expires. Holding one on this object would guarantee a
 * dead stream on the second look.
 */
export interface BrowserLoginRequest {
  interruptId: string;
  toolUseId: string;
  /** Conversation id. The browser session is resolved server-side from it. */
  sessionId: string;
  browserSessionId: string;
  browserId: string;
  viewport: { width: number; height: number };
  deadlineAt?: string;
  targetUrl?: string;
  reason?: string;
  /** Origin to frame the viewer from; empty when undeployed. */
  sandboxOrigin?: string;
  receivedAt: number;
  /** Assistant message whose tool call raised this, for inline anchoring. */
  messageId?: string;
}

/**
 * A freshly minted Live View, valid until `expiresAt`.
 *
 * `controlState` is `'user'` while the automation stream is DISABLED at the
 * service — the only time the human can actually drive. When it reads
 * `'agent'` the viewer is a read-only window onto what the agent is doing.
 */
export interface BrowserLiveView {
  url: string;
  expiresAt: string;
  viewport: { width: number; height: number };
  controlState: 'user' | 'agent';
}

/**
 * The payload posted back to resume the turn.
 *
 * Deliberately an object and never `null`, for the same reason
 * `UserQuestionResponse` is: the backend's `ToolContext.interrupt` only treats
 * a non-null response as an answer, so posting null re-raises the same
 * interrupt forever and strands the user in a paused turn.
 */
export type BrowserLoginResponse =
  | { completed: true; note?: string }
  | { skipped: true; note?: string };

export type BrowserLoginResumeHandler = (
  interruptId: string,
  response: BrowserLoginResponse,
  context?: { sessionId?: string },
) => void | Promise<void>;

/**
 * Tracks browser sign-in prompts from the SSE stream and mints the short-lived
 * Live View URLs the viewer frames.
 *
 * The stream parser calls {@link requestLogin} on `browser_login_required`;
 * the UI renders a prompt bound to {@link pending}, calls {@link mintLiveView}
 * when the user opens the viewer, and {@link resolve} when they finish or
 * decline — which resumes the same turn through the registered handler.
 */
@Injectable({ providedIn: 'root' })
export class BrowserLoginService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly requests = signal<Map<string, BrowserLoginRequest>>(new Map());

  // Interrupt ids already surfaced, so a stream replay or network retry does
  // not resurrect a resolved prompt. Every takeover gets a fresh tool-scoped
  // interrupt id, so a genuine second sign-in later in the conversation is not
  // suppressed by this.
  private readonly seenInterruptIds = new Set<string>();

  private resumeHandler: BrowserLoginResumeHandler | null = null;

  readonly pending = computed<BrowserLoginRequest[]>(() =>
    Array.from(this.requests().values()).sort(
      (a, b) => a.receivedAt - b.receivedAt,
    ),
  );

  readonly hasPending = computed<boolean>(() => this.requests().size > 0);

  /** Register a prompt off the SSE stream. Idempotent for the same id. */
  requestLogin(input: Omit<BrowserLoginRequest, 'receivedAt'>): void {
    if (this.seenInterruptIds.has(input.interruptId)) {
      return;
    }
    this.seenInterruptIds.add(input.interruptId);
    this.requests.update((map) => {
      const next = new Map(map);
      next.set(input.interruptId, { ...input, receivedAt: Date.now() });
      return next;
    });
  }

  /**
   * Whether a prompt's sign-in window has already closed.
   *
   * Past the deadline the backend has released the browser and made the
   * session reapable, so offering the viewer would hand the user a stream that
   * is about to disappear. Treated as *open* when there is no deadline or it
   * cannot be parsed — refusing to show a viewer because of an unreadable
   * timestamp is the worse failure.
   */
  hasLapsed(request: BrowserLoginRequest, now: number = Date.now()): boolean {
    if (!request.deadlineAt) {
      return false;
    }
    const deadline = Date.parse(request.deadlineAt);
    return Number.isFinite(deadline) && now > deadline;
  }

  /**
   * Mint a Live View URL for a conversation's browser session.
   *
   * Sends only the conversation id: the browser session is resolved
   * server-side, which is what prevents one user streaming another's browser.
   * Call this again before `expiresAt` — the signature lives at most 300
   * seconds and a sign-in routinely takes longer.
   */
  async mintLiveView(sessionId: string): Promise<BrowserLiveView> {
    // Cookies and the CSRF header come from the global interceptors
    // (`withCredentialsInterceptor`, `csrfInterceptor`); setting them here
    // would duplicate that and drift from every other app-api call.
    return await firstValueFrom(
      this.http.post<BrowserLiveView>(
        `${this.config.appApiUrl()}/sessions/${encodeURIComponent(sessionId)}/browser/live-view`,
        {},
      ),
    );
  }

  /**
   * Resolve a pending prompt and resume the turn. No-op for an unknown id, so
   * a double-click is harmless.
   */
  async resolve(
    interruptId: string,
    response: BrowserLoginResponse,
  ): Promise<void> {
    const request = this.requests().get(interruptId);
    if (!request) {
      return;
    }

    this.requests.update((map) => {
      const next = new Map(map);
      next.delete(interruptId);
      return next;
    });

    if (!this.resumeHandler) {
      console.warn(
        'BrowserLoginService: no resume handler registered; the turn stays paused',
        { interruptId },
      );
      return;
    }

    try {
      await this.resumeHandler(interruptId, response, {
        sessionId: request.sessionId,
      });
    } catch (err) {
      console.error('BrowserLoginService: resume handler failed', err);
    }
  }

  /** The user finished signing in and handed the browser back. */
  async complete(interruptId: string, note?: string): Promise<void> {
    await this.resolve(interruptId, note ? { completed: true, note } : { completed: true });
  }

  /** The user declined, or the window lapsed. */
  async skip(interruptId: string, note?: string): Promise<void> {
    await this.resolve(interruptId, note ? { skipped: true, note } : { skipped: true });
  }

  /** Register the chat layer's resume callback. Single owner, replaces. */
  setResumeHandler(handler: BrowserLoginResumeHandler | null): void {
    this.resumeHandler = handler;
  }
}
