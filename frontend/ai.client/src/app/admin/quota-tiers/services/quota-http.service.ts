import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../../environments/environment';
import {
  QuotaTier,
  QuotaTierCreate,
  QuotaTierUpdate,
  QuotaAssignment,
  QuotaAssignmentCreate,
  QuotaAssignmentUpdate,
  QuotaOverride,
  QuotaOverrideCreate,
  QuotaOverrideUpdate,
  QuotaEvent,
  UserQuotaInfo,
} from '../models/quota.models';

/**
 * HTTP service for quota management API.
 * Communicates with FastAPI backend admin endpoints.
 */
@Injectable({
  providedIn: 'root',
})
export class QuotaHttpService {
  private http = inject(HttpClient);
  private baseUrl = `${environment.appApiUrl}/api/admin/quota`;

  // ========== Quota Tiers ==========

  getTiers(enabledOnly = false): Observable<QuotaTier[]> {
    const params = new HttpParams().set('enabled_only', enabledOnly);
    return this.http.get<QuotaTier[]>(`${this.baseUrl}/tiers`, { params });
  }

  getTier(tierId: string): Observable<QuotaTier> {
    return this.http.get<QuotaTier>(`${this.baseUrl}/tiers/${tierId}`);
  }

  createTier(tier: QuotaTierCreate): Observable<QuotaTier> {
    return this.http.post<QuotaTier>(`${this.baseUrl}/tiers`, tier);
  }

  updateTier(
    tierId: string,
    updates: QuotaTierUpdate
  ): Observable<QuotaTier> {
    return this.http.patch<QuotaTier>(
      `${this.baseUrl}/tiers/${tierId}`,
      updates
    );
  }

  deleteTier(tierId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/tiers/${tierId}`);
  }

  // ========== Quota Assignments ==========

  getAssignments(
    tierId?: string,
    enabledOnly = false
  ): Observable<QuotaAssignment[]> {
    let params = new HttpParams().set('enabled_only', enabledOnly);
    if (tierId) {
      params = params.set('tier_id', tierId);
    }
    return this.http.get<QuotaAssignment[]>(`${this.baseUrl}/assignments`, {
      params,
    });
  }

  getAssignment(assignmentId: string): Observable<QuotaAssignment> {
    return this.http.get<QuotaAssignment>(
      `${this.baseUrl}/assignments/${assignmentId}`
    );
  }

  createAssignment(
    assignment: QuotaAssignmentCreate
  ): Observable<QuotaAssignment> {
    return this.http.post<QuotaAssignment>(
      `${this.baseUrl}/assignments`,
      assignment
    );
  }

  updateAssignment(
    assignmentId: string,
    updates: QuotaAssignmentUpdate
  ): Observable<QuotaAssignment> {
    return this.http.patch<QuotaAssignment>(
      `${this.baseUrl}/assignments/${assignmentId}`,
      updates
    );
  }

  deleteAssignment(assignmentId: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl}/assignments/${assignmentId}`
    );
  }

  // ========== Quota Overrides ==========

  getOverrides(
    userId?: string,
    activeOnly = false
  ): Observable<QuotaOverride[]> {
    let params = new HttpParams().set('active_only', activeOnly);
    if (userId) {
      params = params.set('user_id', userId);
    }
    return this.http.get<QuotaOverride[]>(`${this.baseUrl}/overrides`, {
      params,
    });
  }

  getOverride(overrideId: string): Observable<QuotaOverride> {
    return this.http.get<QuotaOverride>(
      `${this.baseUrl}/overrides/${overrideId}`
    );
  }

  createOverride(
    override: QuotaOverrideCreate
  ): Observable<QuotaOverride> {
    return this.http.post<QuotaOverride>(
      `${this.baseUrl}/overrides`,
      override
    );
  }

  updateOverride(
    overrideId: string,
    updates: QuotaOverrideUpdate
  ): Observable<QuotaOverride> {
    return this.http.patch<QuotaOverride>(
      `${this.baseUrl}/overrides/${overrideId}`,
      updates
    );
  }

  deleteOverride(overrideId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/overrides/${overrideId}`);
  }

  // ========== Quota Events ==========

  getEvents(options: {
    userId?: string;
    tierId?: string;
    eventType?: string;
    limit?: number;
  }): Observable<QuotaEvent[]> {
    let params = new HttpParams();

    if (options.userId) {
      params = params.set('user_id', options.userId);
    }
    if (options.tierId) {
      params = params.set('tier_id', options.tierId);
    }
    if (options.eventType) {
      params = params.set('event_type', options.eventType);
    }
    if (options.limit) {
      params = params.set('limit', options.limit);
    }

    return this.http.get<QuotaEvent[]>(`${this.baseUrl}/events`, { params });
  }

  // ========== User Quota Inspector ==========

  getUserQuotaInfo(
    userId: string,
    email?: string,
    roles?: string[]
  ): Observable<UserQuotaInfo> {
    let params = new HttpParams().set('user_id', userId);

    if (email) {
      params = params.set('email', email);
    }
    if (roles && roles.length > 0) {
      params = params.set('roles', roles.join(','));
    }

    return this.http.get<UserQuotaInfo>(`${this.baseUrl}/users/inspect`, {
      params,
    });
  }
}
