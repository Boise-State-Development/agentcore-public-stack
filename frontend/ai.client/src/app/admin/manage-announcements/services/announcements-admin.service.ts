import { Injectable, computed, inject, resource, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AnnouncementsService } from '../../../services/announcements/announcements.service';
import {
  Announcement,
  AnnouncementCreateRequest,
  AnnouncementListResponse,
  AnnouncementUpdateRequest,
} from '../models/announcement.model';

/**
 * Admin CRUD for feature announcements (`/admin/announcements`).
 *
 * Separate from the root-provided `AnnouncementsService`, which owns the
 * *user* surface — that one is injected by the always-rendered user dropdown,
 * so putting an admin loader on it would fire `GET /admin/announcements/` for
 * every user on every app load. Same reasoning as `UserMenuLinksService`'s
 * gated admin resource, but split into its own service because the admin
 * surface here is much larger than a second resource.
 *
 * Every mutation reloads the admin list **and** the user-facing feed, so an
 * admin who publishes something sees their own What's-New entry appear
 * without a refresh.
 */
@Injectable({ providedIn: 'root' })
export class AnnouncementsAdminService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly userFeed = inject(AnnouncementsService);

  private readonly baseUrl = computed(
    () => `${this.config.appApiUrl()}/admin/announcements`,
  );

  // Gated so the loader only fires once an admin page asks for it.
  private readonly requested = signal(false);

  readonly announcementsResource = resource({
    params: () => (this.requested() ? {} : undefined),
    loader: async () => this.fetchAll(),
  });

  /** Activates the resource. Called by the admin list page. */
  ensureLoaded(): void {
    this.requested.set(true);
  }

  readonly announcements = computed<Announcement[]>(
    () => this.announcementsResource.value()?.announcements ?? [],
  );

  async fetchAll(): Promise<AnnouncementListResponse> {
    return await firstValueFrom(
      this.http.get<AnnouncementListResponse>(`${this.baseUrl()}/`),
    );
  }

  async get(id: string): Promise<Announcement> {
    return await firstValueFrom(
      this.http.get<Announcement>(`${this.baseUrl()}/${id}`),
    );
  }

  async create(data: AnnouncementCreateRequest): Promise<Announcement> {
    const created = await firstValueFrom(
      this.http.post<Announcement>(`${this.baseUrl()}/`, data),
    );
    this.refresh();
    return created;
  }

  async update(
    id: string,
    updates: AnnouncementUpdateRequest,
  ): Promise<Announcement> {
    const updated = await firstValueFrom(
      this.http.patch<Announcement>(`${this.baseUrl()}/${id}`, updates),
    );
    this.refresh();
    return updated;
  }

  /** draft | scheduled → published. */
  async publish(id: string): Promise<Announcement> {
    const result = await firstValueFrom(
      this.http.post<Announcement>(`${this.baseUrl()}/${id}/publish`, {}),
    );
    this.refresh();
    return result;
  }

  /** Stops it showing. Acknowledgements are kept. */
  async archive(id: string): Promise<Announcement> {
    const result = await firstValueFrom(
      this.http.post<Announcement>(`${this.baseUrl()}/${id}/archive`, {}),
    );
    this.refresh();
    return result;
  }

  /**
   * "Show again" — increments `revision`, so everyone's suppression lapses at
   * once (§D4). This is the destructive-feeling one: it re-surfaces the
   * announcement for every targeted user, which is why it is a separate
   * action from editing and why the UI confirms first.
   */
  async revise(id: string): Promise<Announcement> {
    const result = await firstValueFrom(
      this.http.post<Announcement>(`${this.baseUrl()}/${id}/revise`, {}),
    );
    this.refresh();
    return result;
  }

  async remove(id: string): Promise<void> {
    await firstValueFrom(this.http.delete<void>(`${this.baseUrl()}/${id}`));
    this.refresh();
  }

  private refresh(): void {
    this.announcementsResource.reload();
    // The admin is also a user: keep their own What's-New in step.
    this.userFeed.reload();
  }
}
