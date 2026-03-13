import { Injectable, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AccessListResponse, FineTuningGrant } from '../models/fine-tuning-access.models';

/**
 * HTTP service for admin fine-tuning access management API.
 * Communicates with FastAPI backend admin/fine-tuning endpoints.
 */
@Injectable({
  providedIn: 'root',
})
export class FineTuningAdminHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private baseUrl = computed(() => `${this.config.appApiUrl()}/admin/fine-tuning`);

  /** List all fine-tuning access grants. */
  listGrants(): Observable<AccessListResponse> {
    return this.http.get<AccessListResponse>(`${this.baseUrl()}/access`);
  }

  /** Grant fine-tuning access to a user by email. */
  grantAccess(email: string, monthlyQuotaHours: number): Observable<FineTuningGrant> {
    return this.http.post<FineTuningGrant>(`${this.baseUrl()}/access`, {
      email,
      monthly_quota_hours: monthlyQuotaHours,
    });
  }

  /** Update the monthly GPU-hour quota for a user. */
  updateQuota(email: string, monthlyQuotaHours: number): Observable<FineTuningGrant> {
    return this.http.put<FineTuningGrant>(
      `${this.baseUrl()}/access/${encodeURIComponent(email)}`,
      { monthly_quota_hours: monthlyQuotaHours },
    );
  }

  /** Revoke fine-tuning access for a user. */
  revokeAccess(email: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl()}/access/${encodeURIComponent(email)}`,
    );
  }
}
