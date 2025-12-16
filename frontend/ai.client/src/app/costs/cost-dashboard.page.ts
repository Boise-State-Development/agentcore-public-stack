import { Component, ChangeDetectionStrategy, signal, computed, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CostService } from './services/cost.service';
import { DecimalPipe, DatePipe } from '@angular/common';
import { UserCostSummary } from './models/cost-summary.model';

@Component({
  selector: 'app-cost-dashboard-page',
  imports: [FormsModule, DecimalPipe, DatePipe],
  templateUrl: './cost-dashboard.page.html',
  styleUrl: './cost-dashboard.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CostDashboardPage {
  private costService = inject(CostService);

  // Get current month summary from service resource
  readonly costSummary = this.costService.currentMonthSummary;

  // Custom report data (for last 30 days or custom date range)
  readonly customReportData = signal<UserCostSummary | null>(null);

  // Period selector
  readonly selectedPeriodType = signal<'current' | 'last30' | 'custom'>('current');
  readonly customStartDate = signal<string>('');
  readonly customEndDate = signal<string>('');

  // Loading state for custom date range queries
  readonly isLoadingCustomReport = signal<boolean>(false);
  readonly customReportError = signal<string | null>(null);

  // Active data source - switches between current month and custom report
  readonly activeData = computed(() => {
    const periodType = this.selectedPeriodType();
    if (periodType === 'current') {
      return this.costSummary.value();
    } else {
      return this.customReportData();
    }
  });

  // Computed values from active data source
  readonly totalCost = computed(() => this.activeData()?.totalCost ?? 0);
  readonly totalRequests = computed(() => this.activeData()?.totalRequests ?? 0);
  readonly totalInputTokens = computed(() => this.activeData()?.totalInputTokens ?? 0);
  readonly totalOutputTokens = computed(() => this.activeData()?.totalOutputTokens ?? 0);
  readonly totalCacheSavings = computed(() => this.activeData()?.totalCacheSavings ?? 0);
  readonly models = computed(() => this.activeData()?.models ?? []);

  // Computed stats
  readonly averageCostPerRequest = computed(() => {
    const total = this.totalCost();
    const requests = this.totalRequests();
    return requests > 0 ? total / requests : 0;
  });

  readonly totalTokens = computed(() => this.totalInputTokens() + this.totalOutputTokens());

  readonly averageTokensPerRequest = computed(() => {
    const total = this.totalTokens();
    const requests = this.totalRequests();
    return requests > 0 ? Math.round(total / requests) : 0;
  });

  readonly cacheSavingsPercentage = computed(() => {
    const savings = this.totalCacheSavings();
    const cost = this.totalCost();
    const totalWithoutSavings = cost + savings;
    return totalWithoutSavings > 0 ? (savings / totalWithoutSavings) * 100 : 0;
  });

  /**
   * Load cost data for last 30 days
   */
  async loadLast30Days(): Promise<void> {
    this.selectedPeriodType.set('last30');
    this.isLoadingCustomReport.set(true);
    this.customReportError.set(null);

    try {
      const summary = await this.costService.getCostSummaryForLastNDays(30);
      this.customReportData.set(summary);
    } catch (error) {
      console.error('Error loading last 30 days:', error);
      this.customReportError.set('Failed to load cost data for last 30 days');
    } finally {
      this.isLoadingCustomReport.set(false);
    }
  }

  /**
   * Load cost data for custom date range
   */
  async loadCustomDateRange(): Promise<void> {
    const startDate = this.customStartDate();
    const endDate = this.customEndDate();

    if (!startDate || !endDate) {
      this.customReportError.set('Please select both start and end dates');
      return;
    }

    this.selectedPeriodType.set('custom');
    this.isLoadingCustomReport.set(true);
    this.customReportError.set(null);

    try {
      const summary = await this.costService.fetchDetailedReport(startDate, endDate);
      this.customReportData.set(summary);
    } catch (error) {
      console.error('Error loading custom date range:', error);
      this.customReportError.set('Failed to load cost data for the selected date range');
    } finally {
      this.isLoadingCustomReport.set(false);
    }
  }

  /**
   * Reset to current month view
   */
  resetToCurrentMonth(): void {
    this.selectedPeriodType.set('current');
    this.customStartDate.set('');
    this.customEndDate.set('');
    this.customReportError.set(null);
    this.customReportData.set(null);
    this.costService.reloadCurrentMonthSummary();
  }

  /**
   * Format currency
   */
  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 4,
      maximumFractionDigits: 4
    }).format(value);
  }

  /**
   * Format large numbers
   */
  formatNumber(value: number): string {
    return new Intl.NumberFormat('en-US').format(value);
  }
}
