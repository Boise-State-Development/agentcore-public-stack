import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowDownTray,
  heroMagnifyingGlass,
} from '@ng-icons/heroicons/outline';
import { AdminCostStateService } from './services';
import { PeriodSelectorComponent } from './components/period-selector.component';
import {
  SystemSummaryCardComponent,
} from './components/system-summary-card.component';
import { TopUsersTableComponent } from './components/top-users-table.component';
import { TopSessionsTableComponent } from './components/top-sessions-table.component';
import { CostTrendsChartComponent } from './components/cost-trends-chart.component';
import { ModelBreakdownComponent } from './components/model-breakdown.component';
import { SpinnerComponent } from '../../components/spinner/spinner.component';

/**
 * Admin cost dashboard page.
 * Displays system-wide usage metrics, top users, and cost trends.
 */
@Component({
  selector: 'app-admin-costs',
  imports: [
    FormsModule,
    NgIcon,
    PeriodSelectorComponent,
    SystemSummaryCardComponent,
    TopUsersTableComponent,
    TopSessionsTableComponent,
    CostTrendsChartComponent,
    ModelBreakdownComponent,
    SpinnerComponent,
  ],
  providers: [provideIcons({ heroArrowLeft, heroArrowDownTray, heroMagnifyingGlass })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
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
              class="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-300 rounded-sm text-sm font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-gray-800 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700 transition-colors"
            >
              <ng-icon name="heroArrowDownTray" class="size-4" />
              Export
            </button>
          </div>
        </div>
        <!-- Session cost anatomy lookup -->
        <form
          (ngSubmit)="onInspectSession()"
          class="mb-6 flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-center sm:justify-between dark:border-gray-700 dark:bg-gray-800"
        >
          <div>
            <label for="session-lookup" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
              Session Cost Anatomy
            </label>
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">
              Look up per-call cache diagnostics for a session ID.
            </p>
          </div>
          <div class="flex items-center gap-2 sm:w-96">
            <div class="relative flex-1">
              <ng-icon
                name="heroMagnifyingGlass"
                class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                aria-hidden="true"
              />
              <input
                type="text"
                id="session-lookup"
                name="sessionLookup"
                [ngModel]="sessionLookupId()"
                (ngModelChange)="sessionLookupId.set($event)"
                placeholder="Session ID…"
                class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 font-mono text-sm/6 text-gray-900 placeholder:font-sans placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              />
            </div>
            <button
              type="submit"
              [disabled]="!sessionLookupId().trim()"
              class="shrink-0 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Inspect
            </button>
          </div>
        </form>

        @if (loading()) {
          <!-- Loading State -->
          <div class="flex items-center justify-center h-64">
            <div class="flex flex-col items-center gap-4">
              <app-spinner size="xl" label="Loading dashboard data" />
              <p class="text-sm text-gray-500 dark:text-gray-400">
                Loading dashboard data...
              </p>
            </div>
          </div>
        } @else if (error()) {
          <!-- Error State -->
          <div
            class="bg-state-danger-50 dark:bg-state-danger-900/20 border border-state-danger-200 dark:border-state-danger-800 rounded-lg p-6"
          >
            <div class="flex items-start gap-3">
              <div class="shrink-0">
                <svg
                  class="size-5 text-state-danger-400"
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
                <h3 class="text-sm font-medium text-state-danger-800 dark:text-state-danger-200">
                  Failed to load dashboard
                </h3>
                <p class="mt-1 text-sm text-state-danger-700 dark:text-state-danger-300">
                  {{ error() }}
                </p>
                <button
                  type="button"
                  (click)="loadDashboard()"
                  class="mt-3 text-sm font-medium text-state-danger-600 dark:text-state-danger-400 hover:text-state-danger-500 dark:hover:text-state-danger-300"
                >
                  Try again
                </button>
              </div>
            </div>
          </div>
        } @else {
          <!-- Summary Cards -->
          <div class="grid grid-cols-1 gap-6 sm:grid-cols-2 xl:grid-cols-4">
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

          <!-- Most expensive conversations -->
          <div class="mt-8">
            <app-top-sessions-table
              [sessions]="topSessions()"
              [loading]="loadingTopSessions()"
              [truncated]="topSessionsTruncated()"
              [hasLoaded]="topSessionsLoaded()"
              (load)="onLoadTopSessions()"
              (sessionClick)="onSessionClick($event)"
            />
          </div>
        }
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
  topSessions = this.stateService.topSessions;
  topSessionsTruncated = this.stateService.topSessionsTruncated;
  loadingTopSessions = this.stateService.loadingTopSessions;
  trends = this.stateService.trends;
  modelUsage = this.stateService.modelUsage;

  // Session-id lookup for the cost-anatomy drill-down
  sessionLookupId = signal('');

  // The expensive-conversations scan is on demand (one query per scanned
  // user), so the table needs to tell "not loaded yet" from "nothing found".
  topSessionsLoaded = signal(false);

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
    // The expensive-conversations list is period-scoped; drop it rather than
    // leave last period's rows under a new period's heading.
    this.topSessionsLoaded.set(false);
    this.stateService.topSessions.set([]);
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

  async onLoadTopSessions(): Promise<void> {
    try {
      await this.stateService.loadTopSessions({ limit: 25 });
      this.topSessionsLoaded.set(true);
    } catch {
      // Error is handled by state service
    }
  }

  onSessionClick(sessionId: string): void {
    this.router.navigate(['/admin/costs/sessions', sessionId]);
  }

  onInspectSession(): void {
    const sessionId = this.sessionLookupId().trim();
    if (sessionId) {
      this.router.navigate(['/admin/costs/sessions', sessionId]);
    }
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
