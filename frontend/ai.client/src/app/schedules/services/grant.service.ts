import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { GrantApiService } from './grant-api.service';
import { GrantStatus } from '../models/grant.model';

const DISABLED_STATUS: GrantStatus = { enabled: false };

/** Signal-based state for the caller's headless-grant status. */
@Injectable({
  providedIn: 'root',
})
export class GrantService {
  private apiService = inject(GrantApiService);

  private status = signal<GrantStatus>(DISABLED_STATUS);
  private loading = signal<boolean>(false);
  private error = signal<string | null>(null);

  readonly status$ = this.status.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();

  async loadStatus(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const status = await firstValueFrom(this.apiService.status());
      this.status.set(status);
    } catch (err) {
      // A 403/404 here means the caller shouldn't be on this page at all —
      // the schedules list call is the source of truth for that gate, so
      // just leave the grant looking disabled rather than erroring twice.
      this.status.set(DISABLED_STATUS);
      const errorMessage = err instanceof Error ? err.message : 'Failed to load grant status';
      this.error.set(errorMessage);
    } finally {
      this.loading.set(false);
    }
  }

  async enable(): Promise<GrantStatus> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const status = await firstValueFrom(this.apiService.enable());
      this.status.set(status);
      return status;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to enable scheduled runs';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async revoke(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      await firstValueFrom(this.apiService.revoke());
      this.status.set(DISABLED_STATUS);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to revoke scheduled runs';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }
}
