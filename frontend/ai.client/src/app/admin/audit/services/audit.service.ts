import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { AuditActionsResponse, AuditPage } from '../models/audit.model';

/**
 * Reads the administrative audit trail.
 *
 * Read-only on purpose — the API exposes no mutating routes, and records age
 * out via the table's TTL and by no other means. If a write method ever appears
 * here, something has gone wrong upstream.
 */
@Injectable({ providedIn: 'root' })
export class AuditService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(
    () => `${this.config.appApiUrl()}/admin/audit`
  );

  /**
   * Recent activity for one month, newest first.
   *
   * Pagination is *within* a month — the backing partition is month-sharded, so
   * running out of pages means asking for the previous month rather than
   * following a cursor across the boundary.
   */
  async fetchRecent(options: {
    month?: string;
    cursor?: string | null;
    limit?: number;
  } = {}): Promise<AuditPage> {
    let params = new HttpParams();
    if (options.month) params = params.set('month', options.month);
    if (options.cursor) params = params.set('cursor', options.cursor);
    if (options.limit) params = params.set('limit', String(options.limit));

    return firstValueFrom(
      this.http.get<AuditPage>(`${this.baseUrl()}/`, { params })
    );
  }

  /** Full history for one target — "what has happened to this role?". */
  async fetchForTarget(
    targetId: string,
    options: { cursor?: string | null; limit?: number } = {}
  ): Promise<AuditPage> {
    let params = new HttpParams();
    if (options.cursor) params = params.set('cursor', options.cursor);
    if (options.limit) params = params.set('limit', String(options.limit));

    return firstValueFrom(
      this.http.get<AuditPage>(
        `${this.baseUrl()}/targets/${encodeURIComponent(targetId)}`,
        { params }
      )
    );
  }

  /** Everything one admin did — "what has this person been doing?". */
  async fetchForActor(
    actorUserId: string,
    options: { cursor?: string | null; limit?: number } = {}
  ): Promise<AuditPage> {
    let params = new HttpParams();
    if (options.cursor) params = params.set('cursor', options.cursor);
    if (options.limit) params = params.set('limit', String(options.limit));

    return firstValueFrom(
      this.http.get<AuditPage>(
        `${this.baseUrl()}/actors/${encodeURIComponent(actorUserId)}`,
        { params }
      )
    );
  }

  /** The closed action set, for the filter control. */
  async fetchActions(): Promise<AuditActionsResponse> {
    return firstValueFrom(
      this.http.get<AuditActionsResponse>(`${this.baseUrl()}/actions`)
    );
  }
}
