import { Injectable, inject, resource } from '@angular/core';
import { HttpClient, HttpContext } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from './config.service';
import { SUPPRESS_ERROR_TOAST } from '../auth/error.interceptor';

export interface UserSettings {
  defaultModelId: string | null;
  /** User-level default for the skills/tools mode toggle. */
  preferredAgentMode?: 'skill' | 'chat' | null;
}

@Injectable({
  providedIn: 'root'
})
export class UserSettingsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = () => `${this.config.appApiUrl()}/users/me/settings`;

  readonly settingsResource = resource({
    loader: async () => this.fetchSettings(),
  });

  async fetchSettings(): Promise<UserSettings> {
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
    this.settingsResource.reload();
    return result;
  }
}
