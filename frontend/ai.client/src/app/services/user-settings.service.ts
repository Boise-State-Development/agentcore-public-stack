import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from './config.service';

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

  readonly settingsResource = resource({
    loader: async () => this.fetchSettings(),
  });

  async fetchSettings(): Promise<UserSettings> {
    return firstValueFrom(
      this.http.get<UserSettings>(this.baseUrl())
    );
  }

  async updateSettings(settings: Partial<UserSettings>): Promise<UserSettings> {
    const result = await firstValueFrom(
      this.http.put<UserSettings>(this.baseUrl(), settings)
    );
    this.settingsResource.reload();
    return result;
  }
}
