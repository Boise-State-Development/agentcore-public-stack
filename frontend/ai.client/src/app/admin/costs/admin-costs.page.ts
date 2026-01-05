import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowDownTray,
} from '@ng-icons/heroicons/outline';
import { AdminCostStateService } from './services';
import { PeriodSelectorComponent } from './components/period-selector.component';
import {
  SystemSummaryCardComponent,
  SummaryCardIcon,
} from './components/system-summary-card.component';
import { TopUsersTableComponent } from './components/top-users-table.component';
import { CostTrendsChartComponent } from './components/cost-trends-chart.component';
import { ModelBreakdownComponent } from './components/model-breakdown.component';

/**
 * Admin cost dashboard page.
 * Displays system-wide usage metrics, top users, and cost trends.
 */
@Component({
  selector: 'app-admin-costs',
  imports: [
    RouterLink,
    NgIcon,
    PeriodSelectorComponent,
    SystemSummaryCardComponent,
    TopUsersTableComponent,
    CostTrendsChartComponent,
    ModelBreakdownComponent,
  ],
  providers: [provideIcons({ heroArrowLeft, heroArrowDownTray })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="min-h-dvh bg-gray-50 dark:bg-gray-900">
      <!-- Content -->
      <div class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Back Button -->
        <a
          routerLink="/admin"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Admin
        </a>

        <!-- Page Header -->
        <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">
              Cost Analytics
            </h1>
            <p class="mt-1 text-gray-600 dark:text-gray-400">
              Monitor system-wide usage, costs, and trends.
            </p>
          </div>

          <div class="flex items-center gap-4">
            <app-period-selector
              [selectedPeriod]="selectedPeriod()"
              (periodChange)="onPeriodChange($event)"
            />
            <button
              type="button"
              (click)="onExport()"
              [disabled]="loading()"
              class="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-300 rounded-sm text-sm font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-gray-800 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700 transition-colors"
            >
              <ng-icon name="heroArrowDownTray" class="size-4" />
              Export
            </button>
          </div>
        </div>
        @if (loading()) {
          <!-- Loading State -->
          <div class="flex items-center justify-center h-64">
            <div class="flex flex-col items-center gap-4">
              <div
                class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"
              ></div>
              <p class="text-sm text-gray-500 dark:text-gray-400">
                Loading dashboard data...
              </p>
            </div>
          </div>
        } @else if (error()) {
          <!-- Error State -->
          <div
            class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-6"
          >
            <div class="flex items-start gap-3">
              <div class="shrink-0">
                <svg
                  class="size-5 text-red-400"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                >
                  <path
                    fill-rule="evenodd"
                    d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z"
                    clip-rule="evenodd"
                  />
                </svg>
              </div>
              <div>
                <h3 class="text-sm font-medium text-red-800 dark:text-red-200">
                  Failed to load dashboard
                </h3>
                <p class="mt-1 text-sm text-red-700 dark:text-red-300">
                  {{ error() }}
                </p>
                <button
                  type="button"
                  (click)="loadDashboard()"
                  class="mt-3 text-sm font-medium text-red-600 dark:text-red-400 hover:text-red-500 dark:hover:text-red-300"
                >
                  Try again
                </button>
              </div>
            </div>
          </div>
        } @else {
          <!-- Summary Cards -->
          <div class="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <app-system-summary-card
              title="Total Cost"
              [value]="formattedTotalCost()"
              [trend]="null"
              icon="heroCurrencyDollar"
            />
            <app-system-summary-card
              title="Avg Cost/User"
              [value]="formattedAvgCostPerUser()"
              [trend]="null"
              icon="heroUserCircle"
            />
            <app-system-summary-card
              title="Active Users"
              [value]="formattedActiveUsers()"
              [trend]="null"
              icon="heroUsers"
            />
            <app-system-summary-card
              title="Cache Savings"
              [value]="formattedCacheSavings()"
              [trend]="null"
              icon="heroBolt"
            />
          </div>

          <!-- Charts Row -->
          <div class="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-2">
            <app-cost-trends-chart [data]="trends()" />
            <app-model-breakdown [data]="modelUsage()" />
          </div>

          <!-- Top Users Table -->
          <div class="mt-8">
            <app-top-users-table
              [users]="topUsers()"
              [loading]="loadingTopUsers()"
              [hasMore]="hasMoreUsers()"
              (userClick)="onUserClick($event)"
              (loadMore)="onLoadMoreUsers()"
            />
          </div>
        }
      </div>
    </div>
  `,
})
export class AdminCostsPage implements OnInit {
  private stateService = inject(AdminCostStateService);
  private router = inject(Router);

  // State from service
  loading = this.stateService.loading;
  loadingTopUsers = this.stateService.loadingTopUsers;
  error = this.stateService.error;
  selectedPeriod = this.stateService.selectedPeriod;
  topUsers = this.stateService.topUsers;
  topUsersCount = this.stateService.topUsersCount;
  trends = this.stateService.trends;
  modelUsage = this.stateService.modelUsage;

  // Track pagination state for top users
  private topUsersLimit = signal(20);
  hasMoreUsers = computed(
    () => this.topUsers().length >= this.topUsersLimit()
  );

  // Formatted values for display
  formattedTotalCost = computed(() => {
    const cost = this.stateService.totalCost();
    return this.formatCurrency(cost);
  });

  formattedAvgCostPerUser = computed(() => {
    const cost = this.stateService.totalCost();
    const users = this.stateService.activeUsers();
    if (users === 0) return this.formatCurrency(0);
    return this.formatCurrency(cost / users);
  });

  formattedActiveUsers = computed(() => {
    const users = this.stateService.activeUsers();
    return this.formatNumber(users);
  });

  formattedCacheSavings = computed(() => {
    const savings = this.stateService.cacheSavings();
    return this.formatCurrency(savings);
  });

  ngOnInit(): void {
    this.loadDashboard();
  }

  async loadDashboard(): Promise<void> {
    try {
      await this.stateService.loadDashboard({
        topUsersLimit: this.topUsersLimit(),
        includeTrends: true,
      });
    } catch {
      // Error is handled by state service
    }
  }

  onPeriodChange(period: string): void {
    this.stateService.setPeriod(period);
    this.loadDashboard();
  }

  async onExport(): Promise<void> {
    try {
      await this.stateService.exportData('csv');
    } catch {
      // Error is handled by state service
    }
  }

  onUserClick(userId: string): void {
    this.router.navigate(['/admin/users', userId]);
  }

  async onLoadMoreUsers(): Promise<void> {
    const newLimit = this.topUsersLimit() + 20;
    this.topUsersLimit.set(newLimit);

    try {
      await this.stateService.loadTopUsers({
        limit: newLimit,
      });
    } catch {
      // Error is handled by state service
    }
  }

  private formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  }

  private formatNumber(value: number): string {
    return new Intl.NumberFormat('en-US').format(value);
  }
}
