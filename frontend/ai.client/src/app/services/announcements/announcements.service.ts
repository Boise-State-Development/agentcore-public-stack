import { Injectable, computed, inject, resource, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import {
  Announcement,
  AnnouncementAckAction,
  AnnouncementAckRequest,
  AnnouncementFeed,
  AnnouncementSurface,
} from './announcement.model';

const EMPTY_FEED: AnnouncementFeed = {
  panel: [],
  banner: null,
  modal: null,
  unread_count: 0,
};

/**
 * Feature announcements — the read side and the acknowledgement side.
 *
 * **The server decides what is visible; this service renders what it is
 * handed** (§D5). There is deliberately no targeting, date, or ack filtering
 * here: those rules exist once, in `apis/shared/announcements/visibility.py`.
 *
 * Two pieces of local state exist on top of the server's answer, and both are
 * conveniences rather than truth:
 *
 * - `locallyHidden` implements the **fail-open dismissal** from §D7. If the
 *   ack POST fails, the item is hidden for this tab anyway. A user trapped
 *   under a banner they cannot dismiss because of a transient 500 is a worse
 *   outcome than an announcement that comes back tomorrow.
 * - `locallyRead` clears the unread dot the moment the panel opens, without
 *   waiting for the round trip. The server is still told, and the next fetch
 *   agrees.
 *
 * Neither is ever the source of truth — that is the whole point of §D3.
 */
@Injectable({ providedIn: 'root' })
export class AnnouncementsService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly baseUrl = computed(
    () => `${this.config.appApiUrl()}/announcements`,
  );

  /**
   * Loads on first read. The topnav only renders the user dropdown once the
   * session bootstrap has resolved, so the loader fires post-auth with the
   * user's roles known — which the server needs to evaluate targeting.
   */
  readonly feedResource = resource({
    loader: async () => this.fetchFeed(),
  });

  /** Dismissed in this tab, whether or not the server agreed. */
  private readonly locallyHidden = signal<ReadonlySet<string>>(new Set());
  /** Read in this tab, so the dot clears before the round trip lands. */
  private readonly locallyRead = signal<ReadonlySet<string>>(new Set());

  private readonly feed = computed<AnnouncementFeed>(
    () => this.feedResource.value() ?? EMPTY_FEED,
  );

  /** Every announcement this user may browse, newest first. Uncapped. */
  readonly panelItems = computed<Announcement[]>(() => this.feed().panel);

  /** At most one, chosen by the server. Consumed by PR-4. */
  readonly bannerItem = computed<Announcement | null>(() =>
    this.visibleOrNull(this.feed().banner),
  );

  /** At most one, chosen by the server. Consumed by PR-5. */
  readonly modalItem = computed<Announcement | null>(() =>
    this.visibleOrNull(this.feed().modal),
  );

  readonly unreadCount = computed<number>(() => {
    const read = this.locallyRead();
    return this.panelItems().filter(a => a.is_unread && !read.has(a.announcement_id))
      .length;
  });

  readonly hasUnread = computed<boolean>(() => this.unreadCount() > 0);

  /** Whether an item should render with a "New" / "Updated" pill. */
  isUnread(announcement: Announcement): boolean {
    return announcement.is_unread && !this.locallyRead().has(announcement.announcement_id);
  }

  /**
   * Record an acknowledgement.
   *
   * Fails open (§D7): a rejected POST still hides the item locally and
   * resolves rather than throwing, so no caller has to remember to catch.
   * Returns whether the server accepted it, for tests and for a future retry.
   */
  async ack(
    announcementId: string,
    action: AnnouncementAckAction,
    surface: AnnouncementSurface,
  ): Promise<boolean> {
    if (action !== 'seen') {
      this.locallyHidden.update(prev => new Set(prev).add(announcementId));
    }
    this.locallyRead.update(prev => new Set(prev).add(announcementId));

    const body: AnnouncementAckRequest = { action, surface };
    try {
      await firstValueFrom(
        this.http.post<void>(`${this.baseUrl()}/${announcementId}/ack`, body),
      );
      return true;
    } catch {
      // Deliberately swallowed. The local hide already happened, and the next
      // fetch re-derives the truth from the server.
      return false;
    }
  }

  /**
   * Mark every unread panel item `seen` — what opening the What's-New panel
   * means. Fire-and-forget per item, since each `ack` already fails open.
   */
  async markPanelSeen(): Promise<void> {
    const unread = this.panelItems().filter(a => this.isUnread(a));
    if (unread.length === 0) return;
    await Promise.all(
      unread.map(a => this.ack(a.announcement_id, 'seen', 'panel')),
    );
  }

  /** Re-read the server's answer. */
  reload(): void {
    this.feedResource.reload();
  }

  private visibleOrNull(item: Announcement | null): Announcement | null {
    if (!item) return null;
    return this.locallyHidden().has(item.announcement_id) ? null : item;
  }

  private async fetchFeed(): Promise<AnnouncementFeed> {
    try {
      return await firstValueFrom(
        this.http.get<AnnouncementFeed>(`${this.baseUrl()}/`),
      );
    } catch {
      // The surface is kill-switched off (404) or the backend is unhappy.
      // Announcements are ambient; none of this is worth an error to the user.
      return EMPTY_FEED;
    }
  }
}
