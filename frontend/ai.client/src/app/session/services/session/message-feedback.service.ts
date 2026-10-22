import { computed, inject, Injectable, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { ComposerDraftService } from './composer-draft.service';
import { Message, MessageFeedback, FeedbackReason, ImplicitSignalKind } from '../models/message.model';

/**
 * Thumbs up / down on assistant messages — the outcome signal joined to the
 * cost rows (docs/specs/document-context-offload.md §5 row 7).
 *
 * Content-free by construction: the wire carries `value` (+1 / -1) and an
 * optional `reason` code from a closed enum. There is no text field anywhere
 * in this service, so nothing a user types can reach the metadata table.
 *
 * State is optimistic: a click flips the local value immediately, the PUT /
 * DELETE runs after, and a failure rolls the local value back to what the
 * server last confirmed. On reload the value comes back on the message's
 * metadata (`metadata.feedback`, merged from the `F#` row by `GET /messages`),
 * which `feedbackFor` prefers unless a local click has overridden it.
 */
@Injectable({ providedIn: 'root' })
export class MessageFeedbackService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly composerDraft = inject(ComposerDraftService);

  /**
   * A down-thumb the user chose to retry, waiting for the correction to be
   * sent. Consumed by `consumePendingRetry` when the next user message of
   * that session is added, which is when the row gets its `retryMessageId`.
   */
  private readonly pendingRetry = signal<{ sessionId: string; message: Message } | null>(null);

  /** Local overrides keyed by message id; `null` = withdrawn. */
  private readonly overrides = signal<ReadonlyMap<string, MessageFeedback | null>>(new Map());

  /** Message ids with a request in flight (disables the pair while pending). */
  private readonly pending = signal<ReadonlySet<string>>(new Set());

  /**
   * Set once the backend answers 404 with the kill switch off: the surface
   * does not exist in this environment, so the buttons hide.
   */
  readonly unavailable = signal(false);

  readonly isPending = computed(() => (id: string) => this.pending().has(id));

  /** The effective thumb for a message: a local override wins, else what
   *  the server sent with the message. */
  feedbackFor(message: Message): MessageFeedback | null {
    const overrides = this.overrides();
    if (overrides.has(message.id)) return overrides.get(message.id) ?? null;
    return readPersistedFeedback(message);
  }

  /** Thumb a message, replacing any earlier thumb (one per user+message). */
  async setFeedback(
    message: Message,
    value: 1 | -1,
    reason?: FeedbackReason,
    retryMessageId?: number,
  ): Promise<void> {
    const target = parseMessageRef(message.id);
    if (!target) return;
    const previous = this.feedbackFor(message);
    const optimistic: MessageFeedback = {
      value,
      reason: reason ?? (previous?.value === value ? previous?.reason : undefined),
      retryMessageId: retryMessageId ?? previous?.retryMessageId,
      updatedAt: new Date().toISOString(),
    };
    this.setOverride(message.id, optimistic);
    this.markPending(message.id, true);
    try {
      const body: { value: 1 | -1; reason?: FeedbackReason; retryMessageId?: number } = { value };
      if (optimistic.reason) body.reason = optimistic.reason;
      if (retryMessageId !== undefined) body.retryMessageId = retryMessageId;
      const stored = await firstValueFrom(
        this.http.put<MessageFeedback>(this.url(target.sessionId, target.index), body),
      );
      this.setOverride(message.id, stored);
    } catch (error) {
      this.rollback(message.id, previous, error);
    } finally {
      this.markPending(message.id, false);
    }
  }

  /**
   * The consequence (spec §7 "the retry loop"): put a correction template for
   * the thumb's reason into the composer for the user to edit and send. The
   * template is conversation content and goes where the message goes;
   * nothing of it is stored on the feedback row.
   */
  requestRetry(message: Message): void {
    const target = parseMessageRef(message.id);
    if (!target) return;
    const reason = this.feedbackFor(message)?.reason;
    this.pendingRetry.set({ sessionId: target.sessionId, message });
    this.composerDraft.request(target.sessionId, retryTemplate(reason));
  }

  /**
   * Called by the send path once the next user message of a session exists.
   * Links it to the pending down-thumb as `retryMessageId` — an index, never
   * the correction's text — and clears the pending retry. A message on
   * another session, or with no server-shaped id, leaves the retry pending.
   */
  consumePendingRetry(sessionId: string, userMessage: Message | null | undefined): void {
    const pending = this.pendingRetry();
    if (!pending || pending.sessionId !== sessionId || !userMessage) return;
    const sent = parseMessageRef(userMessage.id);
    if (!sent) return;
    this.pendingRetry.set(null);
    const current = this.feedbackFor(pending.message);
    if (!current || current.value !== -1) return;
    void this.setFeedback(pending.message, -1, current.reason, sent.index);
  }

  /** `messageId:kind` pairs already sent this page load — one signal per
   *  message per kind per visit is all the read model counts anyway. */
  private readonly signalled = new Set<string>();

  /**
   * Implicit signal (spec §10): copy or continue on an assistant message.
   * Fire-and-forget — nothing in the UI depends on it, so a failure is
   * logged at debug level and never surfaces. Content-free: a kind code.
   */
  recordSignal(message: Message, kind: ImplicitSignalKind): void {
    const target = parseMessageRef(message.id);
    if (!target || this.unavailable()) return;
    const key = `${message.id}:${kind}`;
    if (this.signalled.has(key)) return;
    this.signalled.add(key);
    const base = this.config.appApiUrl().replace(/\/$/, '');
    const url = `${base}/sessions/${encodeURIComponent(target.sessionId)}/messages/${target.index}/signals`;
    firstValueFrom(this.http.post<void>(url, { kind })).catch((error) => {
      if (isKillSwitch404(error)) this.unavailable.set(true);
      console.debug('Implicit signal not recorded:', kind, error);
    });
  }

  /** Withdraw the thumb on a message. */
  async clearFeedback(message: Message): Promise<void> {
    const target = parseMessageRef(message.id);
    if (!target) return;
    const previous = this.feedbackFor(message);
    this.setOverride(message.id, null);
    this.markPending(message.id, true);
    try {
      await firstValueFrom(this.http.delete<void>(this.url(target.sessionId, target.index)));
    } catch (error) {
      this.rollback(message.id, previous, error);
    } finally {
      this.markPending(message.id, false);
    }
  }

  private url(sessionId: string, index: number): string {
    const base = this.config.appApiUrl().replace(/\/$/, '');
    return `${base}/sessions/${encodeURIComponent(sessionId)}/messages/${index}/feedback`;
  }

  private rollback(messageId: string, previous: MessageFeedback | null, error: unknown): void {
    if (isKillSwitch404(error)) {
      // MESSAGE_FEEDBACK_ENABLED=false: the surface does not exist here, so
      // hide the pair rather than keep offering a click that cannot land.
      // (A missing session is also a 404, but with a different detail, and
      // must not hide feedback everywhere else.)
      this.unavailable.set(true);
    }
    this.setOverride(messageId, previous);
    console.warn('Message feedback not saved:', error);
  }

  private setOverride(messageId: string, value: MessageFeedback | null): void {
    this.overrides.update((current) => {
      const next = new Map(current);
      next.set(messageId, value);
      return next;
    });
  }

  private markPending(messageId: string, on: boolean): void {
    this.pending.update((current) => {
      const next = new Set(current);
      if (on) next.add(messageId);
      else next.delete(messageId);
      return next;
    });
  }
}

/** `msg-{sessionId}-{index}` → the session and the 0-based message index the
 *  cost row keys on. Splits on the LAST dash so a session id with dashes is
 *  irrelevant. `null` for any id not in that shape (a client-only placeholder). */
export function parseMessageRef(messageId: string): { sessionId: string; index: number } | null {
  if (!messageId.startsWith('msg-')) return null;
  const cut = messageId.lastIndexOf('-');
  if (cut <= 4) return null;
  const index = Number(messageId.slice(cut + 1));
  if (!Number.isInteger(index) || index < 0) return null;
  return { sessionId: messageId.slice(4, cut), index };
}

/** The thumb `GET /messages` merged onto the message's metadata, if any. */
export function readPersistedFeedback(message: Message): MessageFeedback | null {
  const raw = message.metadata?.['feedback'];
  if (!raw || typeof raw !== 'object') return null;
  const value = (raw as { value?: unknown }).value;
  if (value !== 1 && value !== -1) return null;
  const reason = (raw as { reason?: unknown }).reason;
  const retry = (raw as { retryMessageId?: unknown }).retryMessageId;
  return {
    value,
    reason: isFeedbackReason(reason) ? reason : undefined,
    retryMessageId: typeof retry === 'number' && Number.isInteger(retry) && retry >= 0 ? retry : undefined,
    updatedAt: String((raw as { updatedAt?: unknown }).updatedAt ?? ''),
  };
}

/**
 * Correction templates per reason code (spec §6 buckets). Each ends where the
 * user's own words belong; they are prefilled, never auto-sent.
 */
export function retryTemplate(reason: FeedbackReason | undefined): string {
  switch (reason) {
    case 'wrong':
      return 'That answer was wrong or made up. Redo it, checking each claim against the sources you actually have, and say what you are unsure about. Specifically: ';
    case 'instructions':
      return 'That answer ignored my instructions. Redo it following exactly what I asked for. In particular: ';
    case 'length':
      return 'That answer was the wrong length. Redo it ';
    case 'tool_failed':
      return 'A tool or search failed in that answer. Try again, and if it fails again tell me instead of guessing. ';
    case 'outdated':
      return 'That answer was out of date. Redo it using the most current information you have, and say how current it is. ';
    case 'other':
    default:
      return 'That answer did not work for me. Redo it, and this time ';
  }
}

export const FEEDBACK_REASONS: readonly FeedbackReason[] = ['wrong', 'instructions', 'length', 'tool_failed', 'outdated', 'other'];

export function isFeedbackReason(value: unknown): value is FeedbackReason {
  return typeof value === 'string' && (FEEDBACK_REASONS as readonly string[]).includes(value);
}

function isKillSwitch404(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false;
  const { status, error: body } = error as { status?: number; error?: { detail?: unknown } };
  return status === 404 && body?.detail === 'Not found';
}
