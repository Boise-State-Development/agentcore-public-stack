import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { CostService } from './services/cost.service';
import { UserCostSummary } from './models/cost-summary.model';

@Component({
  selector: 'app-usage-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe],
  host: { class: 'block' },
  template: `
    <div class="flex flex-col gap-8">
      <!-- Section header -->
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Usage</h2>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Track your AI usage costs and token consumption.
        </p>
      </div>

      <!-- Period Selector -->
      <div class="flex flex-wrap items-center gap-3">
        <button
          type="button"
          (click)="resetToCurrentMonth()"
          [class]="selectedPeriodType() === 'current'
            ? 'bg-blue-600 text-white border-blue-600 dark:bg-blue-500 dark:border-blue-500'
            : 'bg-white text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-600'"
          class="rounded-sm border px-3 py-1.5 text-sm/6 font-medium transition-colors"
        >
          Current Month
        </button>
        <button
          type="button"
          (click)="loadLast30Days()"
          [class]="selectedPeriodType() === 'last30'
            ? 'bg-blue-600 text-white border-blue-600 dark:bg-blue-500 dark:border-blue-500'
            : 'bg-white text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-600'"
          class="rounded-sm border px-3 py-1.5 text-sm/6 font-medium transition-colors"
        >
          Last 30 Days
        </button>

        <div class="h-5 w-px bg-gray-200 dark:bg-white/10"></div>

        <select
          (change)="loadMonth($any($event.target).value)"
          [value]="selectedPeriodType() === 'month' ? selectedMonthValue() : ''"
          class="rounded-sm border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors focus:border-blue-500 focus:outline-hidden focus:ring-2 focus:ring-blue-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300"
        >
          <option value="" disabled [selected]="selectedPeriodType() !== 'month'">Previous month...</option>
          @for (month of previousMonths; track month.value) {
            <option [value]="month.value">{{ month.label }}</option>
          }
        </select>
      </div>

      <!-- Loading State -->
      @if ((selectedPeriodType() === 'current' && costSummary.isLoading()) || isLoadingCustomReport()) {
        <div class="flex items-center justify-center py-12">
          <div class="flex flex-col items-center gap-3">
            <div class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading cost data...</p>
          </div>
        </div>
      }

      <!-- Error State -->
      @else if (selectedPeriodType() === 'current' && costSummary.error()) {
        <div class="rounded-lg bg-red-50 p-4 dark:bg-red-900/20">
          <p class="text-sm/6 font-medium text-red-800 dark:text-red-200">Error loading cost data</p>
        </div>
      }

      @else if (customReportError()) {
        <div class="rounded-lg bg-red-50 p-4 dark:bg-red-900/20">
          <p class="text-sm/6 text-red-800 dark:text-red-200">{{ customReportError() }}</p>
        </div>
      }

      <!-- Cost Summary Cards -->
      @else if (activeData()) {
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <h3 class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Total Cost</h3>
            <p class="mt-2 text-2xl font-bold text-gray-900 dark:text-white">
              {{ formatCurrency(totalCost()) }}
            </p>
            @if (totalCacheSavings() > 0) {
              <p class="mt-1 text-xs text-green-600 dark:text-green-400">
                Saved {{ formatCurrency(totalCacheSavings()) }} with caching
                ({{ cacheSavingsPercentage() | number: '1.1-1' }}%)
              </p>
            }
          </div>

          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <h3 class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Total Requests</h3>
            <p class="mt-2 text-2xl font-bold text-gray-900 dark:text-white">
              {{ formatNumber(totalRequests()) }}
            </p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Avg: {{ formatCurrency(averageCostPerRequest()) }} per request
            </p>
          </div>

          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <h3 class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Total Tokens</h3>
            <p class="mt-2 text-2xl font-bold text-gray-900 dark:text-white">
              {{ formatNumber(totalTokens()) }}
            </p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Avg: {{ formatNumber(averageTokensPerRequest()) }} per request
            </p>
          </div>

          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <h3 class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Token Breakdown</h3>
            <div class="mt-2 flex flex-col gap-1">
              <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                Input: {{ formatNumber(totalInputTokens()) }}
              </p>
              <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                Output: {{ formatNumber(totalOutputTokens()) }}
              </p>
            </div>
          </div>
        </div>

        <!-- Per-Model Breakdown -->
        @if (models().length > 0) {
          <div>
            <h3 class="mb-3 text-sm/6 font-medium text-gray-900 dark:text-white">Cost by Model</h3>
            <div class="overflow-hidden rounded-lg border border-gray-200 dark:border-white/10">
              <table class="min-w-full divide-y divide-gray-200 dark:divide-white/10">
                <thead class="bg-gray-50 dark:bg-white/5">
                  <tr>
                    <th scope="col" class="px-4 py-3 text-left text-xs/5 font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Model</th>
                    <th scope="col" class="px-4 py-3 text-right text-xs/5 font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Requests</th>
                    <th scope="col" class="px-4 py-3 text-right text-xs/5 font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Tokens</th>
                    <th scope="col" class="px-4 py-3 text-right text-xs/5 font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Cost</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200 bg-white dark:divide-white/10 dark:bg-gray-800">
                  @for (model of models(); track model.modelId) {
                    <tr>
                      <td class="whitespace-nowrap px-4 py-3 text-sm/6 text-gray-900 dark:text-white">{{ model.modelName }}</td>
                      <td class="whitespace-nowrap px-4 py-3 text-right text-sm/6 text-gray-900 dark:text-white">{{ formatNumber(model.requestCount) }}</td>
                      <td class="whitespace-nowrap px-4 py-3 text-right text-sm/6 text-gray-900 dark:text-white">{{ formatNumber(model.totalInputTokens + model.totalOutputTokens) }}</td>
                      <td class="whitespace-nowrap px-4 py-3 text-right text-sm/6 font-medium text-gray-900 dark:text-white">{{ formatCurrency(model.costBreakdown.totalCost) }}</td>
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          </div>
        } @else {
          <div class="rounded-lg border border-gray-200 bg-white p-8 text-center dark:border-white/10 dark:bg-gray-800">
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">No cost data available for this period</p>
          </div>
        }
      }
    </div>
  `,
})
export class UsageSettingsPage {
  private costService = inject(CostService);

  readonly costSummary = this.costService.currentMonthSummary;
  readonly customReportData = signal<UserCostSummary | null>(null);
  readonly selectedPeriodType = signal<'current' | 'last30' | 'month'>('current');
  readonly selectedMonthValue = signal('');
  readonly isLoadingCustomReport = signal(false);
  readonly customReportError = signal<string | null>(null);

  readonly previousMonths = this.buildPreviousMonths(11);

  readonly activeData = computed(() => {
    if (this.selectedPeriodType() === 'current') {
      return this.costSummary.value();
    }
    return this.customReportData();
  });

  readonly periodLabel = computed(() => {
    const type = this.selectedPeriodType();
    if (type === 'current') return 'Current Month';
    if (type === 'last30') return 'Last 30 Days';
    const match = this.previousMonths.find(m => m.value === this.selectedMonthValue());
    return match ? match.label : '';
  });

  readonly totalCost = computed(() => this.activeData()?.totalCost ?? 0);
  readonly totalRequests = computed(() => this.activeData()?.totalRequests ?? 0);
  readonly totalInputTokens = computed(() => this.activeData()?.totalInputTokens ?? 0);
  readonly totalOutputTokens = computed(() => this.activeData()?.totalOutputTokens ?? 0);
  readonly totalCacheSavings = computed(() => this.activeData()?.totalCacheSavings ?? 0);
  readonly models = computed(() => this.activeData()?.models ?? []);

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

  async loadLast30Days(): Promise<void> {
    this.selectedPeriodType.set('last30');
    this.selectedMonthValue.set('');
    this.isLoadingCustomReport.set(true);
    this.customReportError.set(null);

    try {
      const summary = await this.costService.getCostSummaryForLastNDays(30);
      this.customReportData.set(summary);
    } catch {
      this.customReportError.set('Failed to load cost data for last 30 days');
    } finally {
      this.isLoadingCustomReport.set(false);
    }
  }

  async loadMonth(value: string): Promise<void> {
    if (!value) return;
    this.selectedPeriodType.set('month');
    this.selectedMonthValue.set(value);
    this.isLoadingCustomReport.set(true);
    this.customReportError.set(null);

    try {
      const summary = await this.costService.fetchCostSummary(value);
      this.customReportData.set(summary);
    } catch {
      this.customReportError.set('Failed to load cost data for selected month');
    } finally {
      this.isLoadingCustomReport.set(false);
    }
  }

  resetToCurrentMonth(): void {
    this.selectedPeriodType.set('current');
    this.selectedMonthValue.set('');
    this.customReportError.set(null);
    this.customReportData.set(null);
    this.costService.reloadCurrentMonthSummary();
  }

  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    }).format(value);
  }

  formatNumber(value: number): string {
    return new Intl.NumberFormat('en-US').format(value);
  }

  private buildPreviousMonths(count: number): { value: string; label: string }[] {
    const months: { value: string; label: string }[] = [];
    const now = new Date();
    for (let i = 1; i <= count; i++) {
      const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
      const value = `${d.getFullYear()}-${(d.getMonth() + 1).toString().padStart(2, '0')}`;
      const label = d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
      months.push({ value, label });
    }
    return months;
  }
}
