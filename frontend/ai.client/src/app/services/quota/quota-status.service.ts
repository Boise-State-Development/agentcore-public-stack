import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { QuotaStatus } from './quota-status.model';

/**
 * Fetches the authenticated user's own quota status (GET /costs/quota-status).
 *
 * Read-only and side-effect-free on the backend — safe to poll/refresh. Shared
 * by the Usage settings page (the denominator behind the usage/spend numbers)
 * and the composer cost-counter tooltip.
 */
@Injectable({ providedIn: 'root' })
export class QuotaStatusService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly url = computed(
    () => `${this.config.appApiUrl()}/costs/quota-status`,
  );

  /**
   * Reactive resource holding the current quota status. Lazily loaded on first
   * read; call reload() to refresh (e.g. after a conversation adds cost).
   */
  readonly status = resource<QuotaStatus, unknown>({
    loader: () => this.fetch(),
  });

  async fetch(): Promise<QuotaStatus> {
    return firstValueFrom(this.http.get<QuotaStatus>(this.url()));
  }

  reload(): void {
    this.status.reload();
  }
}
