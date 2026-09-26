import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpContext, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../config.service';
import { AppNotification, NotificationsResponse } from './notification.model';

const PAGE_SIZE = 20;

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
