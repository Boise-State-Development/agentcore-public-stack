import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpContext, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../config.service';
import {
  AppNotification,
  InboxAnswer,
  InboxPromptsResponse,
  InboxRefusalCode,
  InboxRespondResponse,
  NotificationResolution,
  NotificationsResponse,
} from './notification.model';

const PAGE_SIZE = 20;

/**
 * A refused prompts read or answer (answer-from-inbox §3.4–3.5): the server's `code` and the
 * sentence it gave, or `unavailable` for a transport failure or a body this build can't read.
 */
export class InboxRefusal extends Error {
  constructor(
    readonly code: InboxRefusalCode,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = 'InboxRefusal';
  }
}

const REFUSAL_CODES: ReadonlySet<string> = new Set(['already_answered', 'expired', 'invalid_answers', 'unavailable']);

/** The refusal behind a failed inbox call. A 404 (the flag off, or not the caller's row) reads as gone. */
export function inboxRefusalOf(error: unknown): InboxRefusal {
  if (error instanceof InboxRefusal) return error;
  if (error instanceof HttpErrorResponse) {
    const detail = (error.error as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === 'object') {
      const { code, message } = detail as { code?: unknown; message?: unknown };
      if (typeof code === 'string' && REFUSAL_CODES.has(code)) {
        const text = typeof message === 'string' && message ? message : 'Couldn’t send the answer. Try again.';
        return new InboxRefusal(code as InboxRefusalCode, text, error.status);
      }
    }
    if (error.status === 404) return new InboxRefusal('already_answered', 'This is no longer waiting for you.', 404);
    if (error.status === 409) return new InboxRefusal('already_answered', 'This was already answered.', 409);
    if (error.status === 410) return new InboxRefusal('expired', 'This expired before it was answered.', 410);
  }
  return new InboxRefusal('unavailable', 'Couldn’t reach the server. Try again.');
}

/**
 * The signed-in user's notification inbox: the bell's unread count and list.
 *
 * Every call opts out of the global error toast. The inbox loads in the
 * background for the badge, and a failed background read is not something to
 * interrupt the user with; the panel says so where it matters.
 *
 * Reads are optimistic where it helps: marking read updates the list and the
 * count at once, and the server is told afterwards. A failed mark is left
 * alone — the next load brings the truth back.
 */
@Injectable({ providedIn: 'root' })
export class NotificationsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/notifications`);
  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  private readonly items = signal<AppNotification[]>([]);
  private readonly unread = signal(0);
  private readonly loadingState = signal(false);
  private readonly errorState = signal<string | null>(null);
  private readonly loaded = signal(false);

  readonly notifications = this.items.asReadonly();
  readonly unreadCount = this.unread.asReadonly();
  readonly loading = this.loadingState.asReadonly();
  readonly error = this.errorState.asReadonly();
  /** True once the first read has answered (successfully or not). */
  readonly ready = this.loaded.asReadonly();

  /** Re-read the newest page and the unread count. */
  async refresh(): Promise<void> {
    this.loadingState.set(true);
    try {
      const params = new HttpParams().set('limit', PAGE_SIZE);
      const page = await firstValueFrom(
        this.http.get<NotificationsResponse>(this.baseUrl(), { ...this.options, params }),
      );
      this.items.set(page.notifications);
      this.unread.set(page.unreadCount);
      this.errorState.set(null);
    } catch {
      this.errorState.set('Notifications couldn’t be loaded.');
    } finally {
      this.loadingState.set(false);
      this.loaded.set(true);
    }
  }

  async markRead(notification: AppNotification): Promise<void> {
    if (notification.readAt) return;
    const readAt = new Date().toISOString();
    this.items.update(list =>
      list.map(n => (n.notificationId === notification.notificationId ? { ...n, readAt } : n)),
    );
    this.unread.update(n => Math.max(0, n - 1));
    try {
      await firstValueFrom(
        this.http.post<void>(`${this.baseUrl()}/${encodeURIComponent(notification.notificationId)}/read`, {}, this.options),
      );
    } catch {
      // The next refresh restores the server's view.
    }
  }

  /**
   * The server marked `marked` entries read by some other route (a handoff's entries, read by
   * opening its conversation). Reflect it in the listed ones that `match`, and in the count.
   *
   * The count drops only by the unread entries this list holds: an entry written since the last
   * read was never in the count, so taking `marked` off it would under-count. When the server
   * marked more than are listed, the rest are either newer or beyond this page, and only a
   * re-read can say which.
   */
  markedElsewhere(match: (n: AppNotification) => boolean, marked: number): void {
    if (marked <= 0) return;
    const readAt = new Date().toISOString();
    let listed = 0;
    this.items.update(list =>
      list.map(n => {
        if (n.readAt || !match(n)) return n;
        listed++;
        return { ...n, readAt };
      }),
    );
    this.unread.update(n => Math.max(0, n - listed));
    if (marked > listed) void this.refresh();
  }

  /**
   * The live prompts behind a needs-user row (answer-from-inbox §3.4), read from the paused
   * session's breadcrumbs. Throws an {@link InboxRefusal}.
   */
  async prompts(notificationId: string): Promise<InboxPromptsResponse> {
    try {
      return await firstValueFrom(
        this.http.get<InboxPromptsResponse>(`${this.baseUrl()}/${encodeURIComponent(notificationId)}/prompts`, this.options),
      );
    } catch (error) {
      throw inboxRefusalOf(error);
    }
  }

  /**
   * Answer every prompt of a paused run at once (§3.5). On the 202 the row reads as claimed at
   * once (`applying`, read), which is what the server just wrote. Throws an {@link InboxRefusal}.
   */
  async respond(notificationId: string, responses: InboxAnswer[]): Promise<InboxRespondResponse> {
    let answer: InboxRespondResponse;
    try {
      answer = await firstValueFrom(
        this.http.post<InboxRespondResponse>(
          `${this.baseUrl()}/${encodeURIComponent(notificationId)}/respond`,
          { responses },
          this.options,
        ),
      );
    } catch (error) {
      throw inboxRefusalOf(error);
    }
    this.settled(notificationId, answer.resolution);
    return answer;
  }

  /**
   * Show a needs-user row as settled before the next read confirms it: the server marks a row
   * read whenever it resolves it, so the badge drops with it. `retry` gives the buttons back and
   * keeps it unread.
   */
  settled(notificationId: string, resolution: NotificationResolution): void {
    let dropped = 0;
    this.items.update(list =>
      list.map(n => {
        if (n.notificationId !== notificationId) return n;
        if (resolution.state === 'retry') return { ...n, resolution };
        if (!n.readAt) dropped++;
        return { ...n, resolution, readAt: n.readAt ?? resolution.at };
      }),
    );
    if (dropped) this.unread.update(n => Math.max(0, n - dropped));
  }

  async markAllRead(): Promise<void> {
    const readAt = new Date().toISOString();
    this.items.update(list => list.map(n => (n.readAt ? n : { ...n, readAt })));
    this.unread.set(0);
    try {
      await firstValueFrom(this.http.post<{ marked: number }>(`${this.baseUrl()}/read-all`, {}, this.options));
    } catch {
      await this.refresh();
    }
  }
}
