import { Injectable, inject, signal, computed } from '@angular/core';
import { AdminCostHttpService } from './admin-cost-http.service';
import {
  AdminCostDashboard,
  TopUserCost,
  SystemCostSummary,
  CostTrend,
  ModelUsageSummary,
  DashboardRequestOptions,
  TopUsersRequestOptions,
  TrendsRequestOptions,
} from '../models';

export const DEMO_PERIOD = 'demo';

/**
 * State management service for admin cost dashboard using signals.
 * Provides reactive state for dashboard data, loading states, and errors.
 */
@Injectable({
  providedIn: 'root',
})
export class AdminCostStateService {
  private http = inject(AdminCostHttpService);

  // ========== State Signals ==========

  dashboard = signal<AdminCostDashboard | null>(null);
  topUsers = signal<TopUserCost[]>([]);
  systemSummary = signal<SystemCostSummary | null>(null);
  trends = signal<CostTrend[]>([]);
  modelUsage = signal<ModelUsageSummary[]>([]);

  selectedPeriod = signal<string>(this.getCurrentPeriod());

  loading = signal(false);
  loadingTopUsers = signal(false);
  loadingTrends = signal(false);

  error = signal<string | null>(null);

  // ========== Computed Signals ==========

  totalCost = computed(() => this.systemSummary()?.totalCost ?? 0);
  totalRequests = computed(() => this.systemSummary()?.totalRequests ?? 0);
  activeUsers = computed(() => this.systemSummary()?.activeUsers ?? 0);
  cacheSavings = computed(() => this.systemSummary()?.totalCacheSavings ?? 0);

  topUsersCount = computed(() => this.topUsers().length);

  hasData = computed(() => this.dashboard() !== null);
  hasTrends = computed(() => this.trends().length > 0);

  // ========== Dashboard Methods ==========

  async loadDashboard(options: DashboardRequestOptions = {}): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const period = options.period ?? this.selectedPeriod();

      // Use demo data when demo period is selected
      if (period === DEMO_PERIOD) {
        const dashboard = this.generateDemoData(options);
        this.dashboard.set(dashboard);
        this.systemSummary.set(dashboard.currentPeriod);
        this.topUsers.set(dashboard.topUsers);
        this.modelUsage.set(dashboard.modelUsage);

        if (dashboard.dailyTrends) {
          this.trends.set(dashboard.dailyTrends);
        }
        return;
      }

      const dashboard = await this.http
        .getDashboard({
          ...options,
          period,
        })
        .toPromise();

      if (dashboard) {
        this.dashboard.set(dashboard);
        this.systemSummary.set(dashboard.currentPeriod);
        this.topUsers.set(dashboard.topUsers);
        this.modelUsage.set(dashboard.modelUsage);

        if (dashboard.dailyTrends) {
          this.trends.set(dashboard.dailyTrends);
        }
      }
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load dashboard');
      throw error;
    } finally {
      this.loading.set(false);
    }
  }

  async loadTopUsers(options: TopUsersRequestOptions = {}): Promise<void> {
    this.loadingTopUsers.set(true);
    this.error.set(null);

    try {
      const period = options.period ?? this.selectedPeriod();

      // Use demo data when demo period is selected
      if (period === DEMO_PERIOD) {
        this.topUsers.set(this.generateDemoTopUsers(options.limit ?? 20));
        return;
      }

      const users = await this.http
        .getTopUsers({ ...options, period })
        .toPromise();

      this.topUsers.set(users || []);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load top users');
      throw error;
    } finally {
      this.loadingTopUsers.set(false);
    }
  }

  async loadSystemSummary(period?: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const targetPeriod = period ?? this.selectedPeriod();
      const summary = await this.http
        .getSystemSummary(targetPeriod, 'monthly')
        .toPromise();

      if (summary) {
        this.systemSummary.set(summary);
      }
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load system summary');
      throw error;
    } finally {
      this.loading.set(false);
    }
  }

  async loadTrends(options: TrendsRequestOptions): Promise<void> {
    this.loadingTrends.set(true);
    this.error.set(null);

    try {
      const trends = await this.http.getTrends(options).toPromise();
      this.trends.set(trends || []);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load trends');
      throw error;
    } finally {
      this.loadingTrends.set(false);
    }
  }

  async exportData(format: 'csv' | 'json' = 'csv'): Promise<void> {
    try {
      const blob = await this.http
        .exportData(this.selectedPeriod(), format)
        .toPromise();

      if (blob) {
        // Create download link
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `cost_report_${this.selectedPeriod()}.${format}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
      }
    } catch (error: any) {
      this.error.set(error.message || 'Failed to export data');
      throw error;
    }
  }

  // ========== Period Selection ==========

  setPeriod(period: string): void {
    this.selectedPeriod.set(period);
  }

  // ========== Utility Methods ==========

  clearError(): void {
    this.error.set(null);
  }

  reset(): void {
    this.dashboard.set(null);
    this.topUsers.set([]);
    this.systemSummary.set(null);
    this.trends.set([]);
    this.modelUsage.set([]);
    this.selectedPeriod.set(this.getCurrentPeriod());
    this.error.set(null);
  }

  private getCurrentPeriod(): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  }

  // ========== Demo Data Generation ==========

  private generateDemoData(options: DashboardRequestOptions = {}): AdminCostDashboard {
    const now = new Date();
    const currentMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;

    return {
      currentPeriod: this.generateDemoSystemSummary(currentMonth),
      topUsers: this.generateDemoTopUsers(options.topUsersLimit ?? 20),
      modelUsage: this.generateDemoModelUsage(),
      dailyTrends: options.includeTrends ? this.generateDemoDailyTrends() : undefined,
    };
  }

  private generateDemoSystemSummary(period: string): SystemCostSummary {
    return {
      period,
      periodType: 'monthly',
      totalCost: 2847.53,
      totalRequests: 45892,
      activeUsers: 949,
      totalInputTokens: 89_450_000,
      totalOutputTokens: 12_340_000,
      totalCacheSavings: 412.87,
      modelBreakdown: {
        'claude-haiku-4-5': { cost: 892.45, requests: 28450 },
        'claude-sonnet-4': { cost: 1245.67, requests: 12340 },
        'claude-opus-4': { cost: 589.21, requests: 4102 },
        'gpt-4o': { cost: 120.20, requests: 1000 },
      },
      lastUpdated: new Date().toISOString(),
    };
  }

  private generateDemoTopUsers(limit: number): TopUserCost[] {
    const demoUsers: TopUserCost[] = [
      { userId: 'user-001', email: 'alex.johnson@university.edu', totalCost: 245.67, totalRequests: 3456, tierName: 'Faculty', quotaLimit: 500, quotaPercentage: 49, lastUpdated: new Date().toISOString() },
      { userId: 'user-002', email: 'maria.garcia@university.edu', totalCost: 198.34, totalRequests: 2890, tierName: 'Graduate', quotaLimit: 200, quotaPercentage: 99, lastUpdated: new Date().toISOString() },
      { userId: 'user-003', email: 'james.wilson@university.edu', totalCost: 156.78, totalRequests: 2234, tierName: 'Faculty', quotaLimit: 500, quotaPercentage: 31, lastUpdated: new Date().toISOString() },
      { userId: 'user-004', email: 'sarah.chen@university.edu', totalCost: 134.56, totalRequests: 1987, tierName: 'Research', quotaLimit: 1000, quotaPercentage: 13, lastUpdated: new Date().toISOString() },
      { userId: 'user-005', email: 'david.kim@university.edu', totalCost: 112.89, totalRequests: 1654, tierName: 'Graduate', quotaLimit: 200, quotaPercentage: 56, lastUpdated: new Date().toISOString() },
      { userId: 'user-006', email: 'emma.brown@university.edu', totalCost: 98.45, totalRequests: 1432, tierName: 'Faculty', quotaLimit: 500, quotaPercentage: 20, lastUpdated: new Date().toISOString() },
      { userId: 'user-007', email: 'michael.lee@university.edu', totalCost: 87.23, totalRequests: 1298, tierName: 'Undergraduate', quotaLimit: 50, quotaPercentage: 100, lastUpdated: new Date().toISOString() },
      { userId: 'user-008', email: 'olivia.martinez@university.edu', totalCost: 76.89, totalRequests: 1123, tierName: 'Staff', quotaLimit: 100, quotaPercentage: 77, lastUpdated: new Date().toISOString() },
      { userId: 'user-009', email: 'william.taylor@university.edu', totalCost: 65.34, totalRequests: 987, tierName: 'Graduate', quotaLimit: 200, quotaPercentage: 33, lastUpdated: new Date().toISOString() },
      { userId: 'user-010', email: 'sophia.anderson@university.edu', totalCost: 54.21, totalRequests: 876, tierName: 'Research', quotaLimit: 1000, quotaPercentage: 5, lastUpdated: new Date().toISOString() },
      { userId: 'user-011', email: 'ethan.thomas@university.edu', totalCost: 48.76, totalRequests: 754, tierName: 'Faculty', quotaLimit: 500, quotaPercentage: 10, lastUpdated: new Date().toISOString() },
      { userId: 'user-012', email: 'ava.jackson@university.edu', totalCost: 42.34, totalRequests: 643, tierName: 'Graduate', quotaLimit: 200, quotaPercentage: 21, lastUpdated: new Date().toISOString() },
      { userId: 'user-013', email: 'noah.white@university.edu', totalCost: 38.90, totalRequests: 567, tierName: 'Undergraduate', quotaLimit: 50, quotaPercentage: 78, lastUpdated: new Date().toISOString() },
      { userId: 'user-014', email: 'isabella.harris@university.edu', totalCost: 34.56, totalRequests: 489, tierName: 'Staff', quotaLimit: 100, quotaPercentage: 35, lastUpdated: new Date().toISOString() },
      { userId: 'user-015', email: 'liam.clark@university.edu', totalCost: 29.87, totalRequests: 412, tierName: 'Graduate', quotaLimit: 200, quotaPercentage: 15, lastUpdated: new Date().toISOString() },
      { userId: 'user-016', email: 'mia.lewis@university.edu', totalCost: 25.43, totalRequests: 378, tierName: 'Faculty', quotaLimit: 500, quotaPercentage: 5, lastUpdated: new Date().toISOString() },
      { userId: 'user-017', email: 'lucas.robinson@university.edu', totalCost: 21.98, totalRequests: 321, tierName: 'Undergraduate', quotaLimit: 50, quotaPercentage: 44, lastUpdated: new Date().toISOString() },
      { userId: 'user-018', email: 'amelia.walker@university.edu', totalCost: 18.67, totalRequests: 276, tierName: 'Research', quotaLimit: 1000, quotaPercentage: 2, lastUpdated: new Date().toISOString() },
      { userId: 'user-019', email: 'mason.hall@university.edu', totalCost: 15.34, totalRequests: 234, tierName: 'Staff', quotaLimit: 100, quotaPercentage: 15, lastUpdated: new Date().toISOString() },
      { userId: 'user-020', email: 'harper.young@university.edu', totalCost: 12.89, totalRequests: 198, tierName: 'Graduate', quotaLimit: 200, quotaPercentage: 6, lastUpdated: new Date().toISOString() },
    ];

    return demoUsers.slice(0, limit);
  }

  private generateDemoModelUsage(): ModelUsageSummary[] {
    return [
      {
        modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
        modelName: 'Claude Haiku 4.5',
        provider: 'Anthropic',
        totalCost: 892.45,
        totalRequests: 28450,
        uniqueUsers: 142,
        avgCostPerRequest: 0.031,
        totalInputTokens: 45_230_000,
        totalOutputTokens: 6_780_000,
      },
      {
        modelId: 'us.anthropic.claude-sonnet-4-20250514-v1:0',
        modelName: 'Claude Sonnet 4',
        provider: 'Anthropic',
        totalCost: 1245.67,
        totalRequests: 12340,
        uniqueUsers: 98,
        avgCostPerRequest: 0.101,
        totalInputTokens: 32_450_000,
        totalOutputTokens: 4_120_000,
      },
      {
        modelId: 'us.anthropic.claude-opus-4-20250514-v1:0',
        modelName: 'Claude Opus 4',
        provider: 'Anthropic',
        totalCost: 589.21,
        totalRequests: 4102,
        uniqueUsers: 45,
        avgCostPerRequest: 0.144,
        totalInputTokens: 8_920_000,
        totalOutputTokens: 1_230_000,
      },
      {
        modelId: 'gpt-4o',
        modelName: 'GPT-4o',
        provider: 'OpenAI',
        totalCost: 120.20,
        totalRequests: 1000,
        uniqueUsers: 23,
        avgCostPerRequest: 0.120,
        totalInputTokens: 2_850_000,
        totalOutputTokens: 210_000,
      },
    ];
  }

  private generateDemoDailyTrends(): CostTrend[] {
    const trends: CostTrend[] = [];
    const now = new Date();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const currentDay = now.getDate();

    // Generate realistic daily patterns with weekend dips
    for (let day = 1; day <= Math.min(currentDay, daysInMonth); day++) {
      const date = new Date(now.getFullYear(), now.getMonth(), day);
      const dayOfWeek = date.getDay();
      const isWeekend = dayOfWeek === 0 || dayOfWeek === 6;

      // Base values with some randomness
      const baseRequests = isWeekend ? 800 : 1800;
      const baseCost = isWeekend ? 45 : 110;
      const baseUsers = isWeekend ? 25 : 65;

      // Add some variance (±20%)
      const variance = () => 0.8 + Math.random() * 0.4;

      trends.push({
        date: `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
        totalCost: Math.round(baseCost * variance() * 100) / 100,
        totalRequests: Math.round(baseRequests * variance()),
        activeUsers: Math.round(baseUsers * variance()),
      });
    }

    return trends;
  }
}
