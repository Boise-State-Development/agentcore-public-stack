import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import { GrantRevokeResponse, GrantStatus } from '../models/grant.model';

/**
 * Thin HTTP layer over `/runs/grant` (app-api, cookie-authed). Backs the
 * schedules page's enablement UX — a schedule cannot fire unattended
 * without an active grant.
 */
@Injectable({
  providedIn: 'root',
})
export class GrantApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/runs/grant`);

  status(): Observable<GrantStatus> {
    return this.http.get<GrantStatus>(this.baseUrl());
  }

  /** Create-on-enable: mints/refreshes the grant from the caller's live session. */
  enable(): Observable<GrantStatus> {
    return this.http.post<GrantStatus>(this.baseUrl(), {});
  }

  revoke(): Observable<GrantRevokeResponse> {
    return this.http.delete<GrantRevokeResponse>(this.baseUrl());
  }
}
