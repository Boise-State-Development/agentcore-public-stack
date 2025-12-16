import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';
import { AuthService } from '../../auth/auth.service';
import { UserCostSummary } from '../models/cost-summary.model';

/**
 * Service for fetching user cost summaries and detailed reports
 *
 * Provides two main query types:
 * 1. Fast summary using pre-aggregated data (monthly periods)
 * 2. Detailed reports for custom date ranges
 */
@Injectable({
  providedIn: 'root'
})
export class CostService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Reactive resource for fetching current month cost summary.
   *
   * This uses the fast path (pre-aggregated data) for <10ms response time.
   * Automatically refetches when manually reloaded.
   */
  readonly currentMonthSummary = resource({
    loader: async () => {
      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      // Fetch current month summary (no period specified = current month)
      return this.fetchCostSummary();
    }
  });

  /**
   * Fetch cost summary for a specific period (fast path)
   *
   * Uses pre-aggregated UserCostSummary table for <10ms response time.
   *
   * @param period - Optional period (YYYY-MM), defaults to current month
   * @returns Promise resolving to UserCostSummary
   * @throws Error if the API request fails
   */
  async fetchCostSummary(period?: string): Promise<UserCostSummary> {
    try {
      const url = period
        ? `${environment.appApiUrl}/costs/summary?period=${period}`
        : `${environment.appApiUrl}/costs/summary`;

      const response = await firstValueFrom(
        this.http.get<UserCostSummary>(url)
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Fetch detailed cost report for custom date range
   *
   * Queries MessageMetadata table for detailed breakdown.
   * Use this for custom date ranges or when detailed per-message data is needed.
   *
   * @param startDate - Start date (YYYY-MM-DD)
   * @param endDate - End date (YYYY-MM-DD)
   * @returns Promise resolving to UserCostSummary
   * @throws Error if the API request fails or date range exceeds 90 days
   */
  async fetchDetailedReport(startDate: string, endDate: string): Promise<UserCostSummary> {
    try {
      const response = await firstValueFrom(
        this.http.get<UserCostSummary>(
          `${environment.appApiUrl}/costs/detailed-report?start_date=${startDate}&end_date=${endDate}`
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Get cost summary for a specific month
   *
   * @param year - Year (e.g., 2025)
   * @param month - Month (1-12)
   * @returns Promise resolving to UserCostSummary
   */
  async getCostSummaryForMonth(year: number, month: number): Promise<UserCostSummary> {
    const period = `${year}-${month.toString().padStart(2, '0')}`;
    return this.fetchCostSummary(period);
  }

  /**
   * Get cost summary for the last N days
   *
   * Uses detailed report endpoint for custom date ranges.
   *
   * @param days - Number of days to look back
   * @returns Promise resolving to UserCostSummary
   */
  async getCostSummaryForLastNDays(days: number): Promise<UserCostSummary> {
    const endDate = new Date();
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - days);

    const formatDate = (date: Date) => date.toISOString().split('T')[0];

    return this.fetchDetailedReport(
      formatDate(startDate),
      formatDate(endDate)
    );
  }

  /**
   * Reload the current month summary resource
   *
   * Useful for refreshing data after a new conversation or message.
   */
  reloadCurrentMonthSummary(): void {
    this.currentMonthSummary.reload();
  }
}
