import { Injectable, inject, resource } from '@angular/core';
import { HttpClient, HttpContext } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from './config.service';
import { SUPPRESS_ERROR_TOAST } from '../auth/error.interceptor';

export interface UserSettings {
  defaultModelId: string | null;
}

@Injectable({
  providedIn: 'root'
})
export class UserSettingsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = () => `${this.config.appApiUrl()}/users/me/settings`;

  /**
   * In-flight/settled read shared by every caller. See `getSettings`.
   * Null means "nothing fetched yet" — the next read issues the request.
   */
  private settingsPromise: Promise<UserSettings> | null = null;

  readonly settingsResource = resource({
    loader: async () => this.getSettings(),
  });

  /**
   * Read the user's settings, reusing the first fetch.
   *
   * WHY: two independent callers want this on first load — `settingsResource`
   * eagerly, the moment this service is injected, and `ModelService` later,
   * once `/models` has landed and it can resolve `defaultModelId` against the
   * catalog. Those land ~280ms apart, so they are sequential rather than
   * concurrent and a single-flight guard would not have caught the second one.
   * Memoizing the promise does, and it collapses any future caller too.
   *
   * A rejected read is not cached: the promise is cleared in the catch so the
   * next caller retries rather than inheriting a failure it cannot see.
   */
  getSettings(): Promise<UserSettings> {
    this.settingsPromise ??= this.fetchSettings().catch((err) => {
      this.settingsPromise = null;
      throw err;
    });
    return this.settingsPromise;
  }

  private async fetchSettings(): Promise<UserSettings> {
    return firstValueFrom(
      this.http.get<UserSettings>(this.baseUrl())
    );
  }

  /**
   * Persist settings. `silent: true` opts out of the global error toast —
   * for best-effort background persists (e.g. the chat-mode toggle) where
   * the in-memory state already applied and a storage-misconfiguration 503
   * shouldn't interrupt the user. Explicit settings-page saves stay loud.
   */
  async updateSettings(
    settings: Partial<UserSettings>,
    options?: { silent?: boolean },
  ): Promise<UserSettings> {
    const context = options?.silent
      ? new HttpContext().set(SUPPRESS_ERROR_TOAST, true)
      : undefined;
    const result = await firstValueFrom(
      this.http.put<UserSettings>(this.baseUrl(), settings, { context })
    );
    // Drop the memo BEFORE reloading, or the resource's loader would be
    // handed back the pre-write value it is reloading to get rid of.
    this.settingsPromise = null;
    this.settingsResource.reload();
    return result;
  }
}
