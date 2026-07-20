import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AdminCostDashboard,
  TopUserCost,
  SystemCostSummary,
  ModelUsageSummary,
  TierUsageSummary,
  CostTrend,
  DashboardRequestOptions,
  TopUsersRequestOptions,
  TrendsRequestOptions,
  SessionCostAnatomy,
} from '../models';

/**
 * HTTP service for admin cost dashboard API.
 * Communicates with FastAPI backend admin/costs endpoints.
 */
@Injectable({
  providedIn: 'root',
})
export class AdminCostHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private baseUrl = computed(() => `${this.config.appApiUrl()}/admin/costs`);

  // ========== Dashboard Endpoints ==========

  /**
   * Get comprehensive admin cost dashboard.
   * Returns system summary, top users, model usage, tier usage, and trends.
   */
  getDashboard(options: DashboardRequestOptions = {}): Observable<AdminCostDashboard> {
    let params = new HttpParams();

    if (options.period) {
      params = params.set('period', options.period);
    }
    if (options.topUsersLimit !== undefined) {
      params = params.set('topUsersLimit', options.topUsersLimit);
    }
    if (options.includeTrends !== undefined) {
      params = params.set('includeTrends', options.includeTrends);
    }

    return this.http.get<AdminCostDashboard>(`${this.baseUrl()}/dashboard`, { params });
  }

  /**
   * Get top users by cost for a period.
   * Uses GSI query for efficient sorted retrieval.
   */
  getTopUsers(options: TopUsersRequestOptions = {}): Observable<TopUserCost[]> {
    let params = new HttpParams();

    if (options.period) {
      params = params.set('period', options.period);
    }
    if (options.limit !== undefined) {
      params = params.set('limit', options.limit);
    }
    if (options.minCost !== undefined) {
      params = params.set('minCost', options.minCost);
    }
    if (options.tierId) {
      params = params.set('tierId', options.tierId);
    }

    return this.http.get<TopUserCost[]>(`${this.baseUrl()}/top-users`, { params });
  }

  /**
   * Get system-wide cost summary.
   * Uses pre-aggregated rollups for fast response.
   */
  getSystemSummary(
    period?: string,
    periodType: 'daily' | 'monthly' = 'monthly'
  ): Observable<SystemCostSummary> {
    let params = new HttpParams().set('periodType', periodType);

    if (period) {
      params = params.set('period', period);
    }

    return this.http.get<SystemCostSummary>(`${this.baseUrl()}/system-summary`, { params });
  }

  /**
   * Get cost breakdown by model.
   * Returns all models with usage in the period, sorted by cost descending.
   */
  getModelUsage(period?: string): Observable<ModelUsageSummary[]> {
    let params = new HttpParams();

    if (period) {
      params = params.set('period', period);
    }

    return this.http.get<ModelUsageSummary[]>(`${this.baseUrl()}/by-model`, { params });
  }

  /**
   * Get cost breakdown by quota tier.
   * Returns usage statistics per tier, including users at limit.
   */
  getTierUsage(period?: string): Observable<TierUsageSummary[]> {
    let params = new HttpParams();

    if (period) {
      params = params.set('period', period);
    }

    return this.http.get<TierUsageSummary[]>(`${this.baseUrl()}/by-tier`, { params });
  }

  /**
   * Get daily cost trends for a date range.
   * Max range: 90 days.
   */
  getTrends(options: TrendsRequestOptions): Observable<CostTrend[]> {
    const params = new HttpParams()
      .set('startDate', options.startDate)
      .set('endDate', options.endDate);

    return this.http.get<CostTrend[]>(`${this.baseUrl()}/trends`, { params });
  }

  /**
   * Get the per-model-call cost anatomy for one session.
   * Returns chronological call rows with cache status, prefix fingerprints,
   * and session-level cache rollups. 404 when the session has no cost rows.
   */
  getSessionCostAnatomy(sessionId: string): Observable<SessionCostAnatomy> {
    return this.http.get<SessionCostAnatomy>(
      `${this.baseUrl()}/sessions/${encodeURIComponent(sessionId)}/calls`
    );
  }

  /**
   * Export cost data for a period.
   * Returns blob for download.
   */
  exportData(period?: string, format: 'csv' | 'json' = 'csv'): Observable<Blob> {
    let params = new HttpParams().set('format', format);

    if (period) {
      params = params.set('period', period);
    }

    return this.http.get(`${this.baseUrl()}/export`, {
      params,
      responseType: 'blob',
    });
  }
}
