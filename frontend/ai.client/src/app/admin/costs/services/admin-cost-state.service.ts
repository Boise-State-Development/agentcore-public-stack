import { Injectable, inject, signal, computed } from '@angular/core';
import { AdminCostHttpService } from './admin-cost-http.service';
import {
  AdminCostDashboard,
  TopUserCost,
  SystemCostSummary,
  CostTrend,
  ModelUsageSummary,
  PlatformCostSummary,
  DashboardRequestOptions,
  TopUsersRequestOptions,
  TopSessionCost,
  TopSessionsRequestOptions,
  TrendsRequestOptions,
} from '../models';

export const DEMO_PERIOD = 'demo';

/**
 * The Demo Month replays one real month of a production deployment: the
 * system totals, per-model usage and daily trend of boisestate.ai for
 * September 2026, as its admin cost endpoints reported them. Aggregates only;
 * the demo top-users list stays fictional.
 */
const DEMO_SNAPSHOT_PERIOD = '2026-09';

const DEMO_MODEL_USAGE: ModelUsageSummary[] = [
  { modelId: 'global.anthropic.claude-sonnet-5', modelName: 'Claude Sonnet 5', provider: 'Anthropic', totalCost: 1507.07, totalRequests: 40902, uniqueUsers: 1665, avgCostPerRequest: 0.037, totalInputTokens: 9_200_038, totalOutputTokens: 44_108_975 },
  { modelId: 'global.anthropic.claude-opus-5', modelName: 'Claude Opus 5', provider: 'Anthropic', totalCost: 120.01, totalRequests: 787, uniqueUsers: 34, avgCostPerRequest: 0.152, totalInputTokens: 1_166_861, totalOutputTokens: 1_476_143 },
  { modelId: 'global.openai.gpt-5.6-sol', modelName: 'GPT-5.6 Sol', provider: 'OpenAI', totalCost: 69.58, totalRequests: 1053, uniqueUsers: 27, avgCostPerRequest: 0.066, totalInputTokens: 1_323_362, totalOutputTokens: 1_086_064 },
  { modelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0', modelName: 'Claude Haiku 4.5', provider: 'Anthropic', totalCost: 43.28, totalRequests: 8756, uniqueUsers: 558, avgCostPerRequest: 0.005, totalInputTokens: 557_054, totalOutputTokens: 2_760_857 },
  { modelId: 'global.openai.gpt-6-sol', modelName: 'GPT-6 Sol', provider: 'OpenAI', totalCost: 33.7, totalRequests: 1246, uniqueUsers: 17, avgCostPerRequest: 0.027, totalInputTokens: 1_156_721, totalOutputTokens: 1_243_586 },
  { modelId: 'global.anthropic.claude-opus-5-5', modelName: 'Claude Opus 5.5', provider: 'Anthropic', totalCost: 31.47, totalRequests: 249, uniqueUsers: 15, avgCostPerRequest: 0.126, totalInputTokens: 189_430, totalOutputTokens: 314_511 },
  { modelId: 'global.openai.gpt-5.6-terra', modelName: 'GPT-5.6 Terra', provider: 'OpenAI', totalCost: 25.35, totalRequests: 700, uniqueUsers: 61, avgCostPerRequest: 0.036, totalInputTokens: 498_912, totalOutputTokens: 674_212 },
  { modelId: 'global.anthropic.claude-sonnet-4-6', modelName: 'Claude Sonnet 4.6', provider: 'Anthropic', totalCost: 19.51, totalRequests: 679, uniqueUsers: 1, avgCostPerRequest: 0.029, totalInputTokens: 2_704_440, totalOutputTokens: 759_947 },
  { modelId: 'openai.gpt-5.4', modelName: 'GPT-5.4', provider: 'OpenAI', totalCost: 15.3, totalRequests: 563, uniqueUsers: 16, avgCostPerRequest: 0.027, totalInputTokens: 3_450_208, totalOutputTokens: 340_694 },
  { modelId: 'global.openai.gpt-5.6-luna', modelName: 'GPT-5.6 Luna', provider: 'OpenAI', totalCost: 2.25, totalRequests: 592, uniqueUsers: 70, avgCostPerRequest: 0.004, totalInputTokens: 653_862, totalOutputTokens: 374_566 },
  { modelId: 'google.gemma-4-31b', modelName: 'Gemma 4 31B', provider: 'Google', totalCost: 0.8, totalRequests: 284, uniqueUsers: 53, avgCostPerRequest: 0.003, totalInputTokens: 5_210_708, totalOutputTokens: 165_235 },
  { modelId: 'global.openai.gpt-6-luna', modelName: 'GPT-6 Luna', provider: 'OpenAI', totalCost: 0.17, totalRequests: 209, uniqueUsers: 21, avgCostPerRequest: 0.001, totalInputTokens: 20_693, totalOutputTokens: 99_436 },
  { modelId: 'global.openai.gpt-5.5', modelName: 'GPT-5.5', provider: 'OpenAI', totalCost: 0.14, totalRequests: 7, uniqueUsers: 1, avgCostPerRequest: 0.02, totalInputTokens: 19_594, totalOutputTokens: 600 },
  { modelId: 'global.moonshotai.kimi-k3', modelName: 'Kimi K3', provider: 'Moonshot AI', totalCost: 0.08, totalRequests: 7, uniqueUsers: 2, avgCostPerRequest: 0.011, totalInputTokens: 830, totalOutputTokens: 2_064 },
];

/** [day of September, cost, requests, active users] */
const DEMO_DAILY_TRENDS: ReadonlyArray<readonly [number, number, number, number]> = [
  [1, 46.95, 1146, 151], [2, 63.4, 1368, 158], [3, 52.29, 1304, 153], [4, 44.16, 1003, 128],
  [5, 8.74, 322, 28], [6, 15.97, 567, 47], [7, 24.9, 585, 59], [8, 56.44, 1388, 142],
  [9, 76.45, 3406, 579], [10, 50.09, 1427, 197], [11, 98.36, 3393, 127], [12, 10.18, 336, 40],
  [13, 34.42, 667, 54], [14, 85.56, 8291, 628], [15, 69.84, 2034, 249], [16, 73.66, 1743, 233],
  [17, 61.08, 1033, 154], [18, 69.7, 1270, 133], [19, 19.73, 531, 48], [20, 31.6, 419, 41],
  [21, 66.38, 1873, 167], [22, 77.43, 1635, 156], [23, 66.72, 1587, 160], [24, 108.82, 2474, 258],
  [25, 100.13, 1638, 139], [26, 34.97, 723, 48], [27, 39.81, 926, 55], [28, 144.46, 6547, 584],
  [29, 129.69, 3299, 239], [30, 106.74, 3299, 239],
];

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
  topSessions = signal<TopSessionCost[]>([]);
  /** True when more users had period cost than the fan-out scanned. */
  topSessionsTruncated = signal(false);
  systemSummary = signal<SystemCostSummary | null>(null);
  trends = signal<CostTrend[]>([]);
  modelUsage = signal<ModelUsageSummary[]>([]);

  /**
   * All-in platform cost for the selected period.
   *
   * Loaded alongside the dashboard rather than on demand: it is one
   * single-partition DynamoDB read (the AWS-side sync already happened
   * overnight), and the Avg Cost/User card on the page header depends on it,
   * so deferring it would make the headline number pop in late.
   */
  platformCosts = signal<PlatformCostSummary | null>(null);
  loadingPlatformCosts = signal<boolean>(false);

  selectedPeriod = signal<string>(this.getCurrentPeriod());

  loading = signal(false);
  loadingTopUsers = signal(false);
  loadingTopSessions = signal(false);
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

  /**
   * Load the period's most expensive conversations.
   *
   * Deliberately not part of `loadDashboard`: the fan-out costs one query
   * per scanned user, so it is loaded on demand rather than on every visit
   * to the page.
   */
  async loadTopSessions(options: TopSessionsRequestOptions = {}): Promise<void> {
    this.loadingTopSessions.set(true);
    this.error.set(null);

    try {
      const period = options.period ?? this.selectedPeriod();

      if (period === DEMO_PERIOD) {
        this.topSessions.set([]);
        this.topSessionsTruncated.set(false);
        return;
      }

      const response = await this.http
        .getTopSessions({ ...options, period })
        .toPromise();

      this.topSessions.set(response?.sessions ?? []);
      this.topSessionsTruncated.set(response?.truncated ?? false);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load top sessions');
      throw error;
    } finally {
      this.loadingTopSessions.set(false);
    }
  }

  /**
   * Load all-in platform cost for a period.
   *
   * Never throws: the platform figures are an enrichment of a page that
   * already works without them. An environment with the sync disabled, or an
   * account where `ce:GetCostAndUsage` is denied, must still render every
   * other tab — so a failure here leaves `platformCosts` null and the UI
   * falls back to inference-only, rather than setting `error` and blanking
   * the dashboard.
   */
  async loadPlatformCosts(period?: string): Promise<void> {
    this.loadingPlatformCosts.set(true);

    try {
      const targetPeriod = period ?? this.selectedPeriod();

      if (targetPeriod === DEMO_PERIOD) {
        this.platformCosts.set(null);
        return;
      }

      const summary = await this.http.getPlatformCosts(targetPeriod).toPromise();
      this.platformCosts.set(summary ?? null);
    } catch {
      this.platformCosts.set(null);
    } finally {
      this.loadingPlatformCosts.set(false);
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
    return {
      currentPeriod: this.generateDemoSystemSummary(),
      topUsers: this.generateDemoTopUsers(options.topUsersLimit ?? 20),
      modelUsage: this.generateDemoModelUsage(),
      dailyTrends: options.includeTrends ? this.generateDemoDailyTrends() : undefined,
    };
  }

  private generateDemoSystemSummary(): SystemCostSummary {
    return {
      period: DEMO_SNAPSHOT_PERIOD,
      periodType: 'monthly',
      totalCost: 1868.7,
      totalRequests: 56234,
      activeUsers: 1762,
      totalInputTokens: 26_318_629,
      totalOutputTokens: 53_863_409,
      totalCacheSavings: 2549.3,
      modelBreakdown: Object.fromEntries(
        DEMO_MODEL_USAGE.map((m) => [m.modelId, { cost: m.totalCost, requests: m.totalRequests }]),
      ),
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
    return DEMO_MODEL_USAGE.map((m) => ({ ...m }));
  }

  private generateDemoDailyTrends(): CostTrend[] {
    return DEMO_DAILY_TRENDS.map(([day, totalCost, totalRequests, activeUsers]) => ({
      date: `${DEMO_SNAPSHOT_PERIOD}-${String(day).padStart(2, '0')}`,
      totalCost,
      totalRequests,
      activeUsers,
    }));
  }
}
