import { HttpClient } from '@angular/common/http';
import { Injectable, inject, resource, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';

/** One signed-in app or terminal (`DeviceView` in app_api/auth/bff/devices_routes.py). */
export interface SignedInDevice {
  deviceId: string;
  /** `native` (the phone app) or `terminal` (the command-line client). */
  kind: string;
  deviceModel?: string | null;
  appVersion?: string | null;
  signedInAt: string;
  lastUsedAt?: string | null;
  expiresAt?: string | null;
  /** The session making the request; never true from the web app, whose sessions aren't listed. */
  current: boolean;
}

interface DevicesResponse {
  devices: SignedInDevice[];
}

/** `DELETE /auth/devices/{deviceId}`. */
export interface DeviceSignOutResult {
  revoked: number;
  /** The user's headless grant still holds the signed-out session's token, so scheduled runs go on. */
  scheduledRunsContinue: boolean;
}

/**
 * The user's signed-in apps and terminals (native-app-auth.md D9).
 *
 * The list lives here, not on the page, so returning to Settings shows it at once. Nothing is
 * fetched until the card asks for it.
 */
@Injectable({ providedIn: 'root' })
export class SignedInDevicesService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly listWanted = signal(false);

  readonly devices = resource({
    params: () => (this.listWanted() ? true : undefined),
    loader: async () => {
      const response = await firstValueFrom(this.http.get<DevicesResponse>(this.baseUrl()));
      return response.devices;
    },
  });

  /** Show the list: the first call loads it, later ones refresh it behind the copy shown. */
  openList(): void {
    if (!this.listWanted()) {
      this.listWanted.set(true);
      return;
    }
    this.devices.reload();
  }

  /** Sign one app or terminal out. Its row leaves the list at once, then the list refreshes. */
  async signOut(deviceId: string): Promise<DeviceSignOutResult> {
    const result = await firstValueFrom(
      this.http.delete<DeviceSignOutResult>(`${this.baseUrl()}/${encodeURIComponent(deviceId)}`),
    );
    if (this.listWanted()) {
      if (this.devices.hasValue()) {
        this.devices.set(this.devices.value().filter((d) => d.deviceId !== deviceId));
      }
      this.devices.reload();
    }
    return result;
  }

  private baseUrl(): string {
    return `${this.config.appApiUrl()}/auth/devices`;
  }
}
