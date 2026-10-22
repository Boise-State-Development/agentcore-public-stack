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
  heroArrowTrendingUp,
  heroChartPie,
  heroChatBubbleLeftRight,
  heroChevronDown,
  heroServerStack,
  heroMagnifyingGlass,
  heroUsers,
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
import { PlatformCostBreakdownComponent } from './components/platform-cost-breakdown.component';
import { SpinnerComponent } from '../../components/spinner/spinner.component';

/** The four views the dashboard's content is split across. */
type CostTab = 'models' | 'trends' | 'platform' | 'users' | 'conversations';

/**
 * Admin cost dashboard page.
 *
 * The period KPIs stay pinned above the tab strip — they are the answer to
 * "how much did this month cost", which is the question every tab is a
 * drill-down of, and they are four numbers rather than a view of their own.
 * Everything below them is one tab per visualisation, so each chart and table
 * gets the console's full content width instead of sharing a two-column grid
 * with a neighbour (a donut and a 20-row table were each rendering at half
 * width, and the trend chart's date axis was the first casualty).
 *
 * Panels are destroyed on switch rather than hidden: the two Chart.js charts
 * re-render from their canvas `viewChild` on mount, and a hidden canvas cannot
 * size itself, so a `[hidden]` panel would come back to a 0×0 chart. Anything
 * that must survive a switch (the loaded rows) already lives in
 * `AdminCostStateService`, not in the panel.
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
    PlatformCostBreakdownComponent,
    SpinnerComponent,
  ],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroArrowDownTray,
      heroArrowTrendingUp,
      heroChartPie,
      heroChatBubbleLeftRight,
      heroChevronDown,
      heroServerStack,
      heroMagnifyingGlass,
      heroUsers,
    }),
  ],
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
            class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroArrowDownTray" class="size-4" />
            Export
          </button>
        </div>
      </div>

      @if (loading()) {
        <!-- Loading State -->
        <div class="flex h-64 items-center justify-center">
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
          class="rounded-2xl border border-state-danger-200 bg-state-danger-50 p-6 dark:border-state-danger-800 dark:bg-state-danger-900/20"
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
                class="mt-3 text-sm font-medium text-state-danger-600 hover:text-state-danger-500 dark:text-state-danger-400 dark:hover:text-state-danger-300"
              >
                Try again
              </button>
            </div>
          </div>
        </div>
      } @else {
        <!-- Period KPIs. Above the tabs on purpose: every tab below is a
             drill-down of these four numbers. -->
        <div class="grid grid-cols-1 gap-6 sm:grid-cols-2 xl:grid-cols-4">
          <app-system-summary-card
            [title]="allInAvailable() ? 'Total Cost (all-in)' : 'Total Cost'"
            [value]="formattedTotalCost()"
            [detail]="totalCostDetail()"
            [trend]="null"
            icon="heroCurrencyDollar"
          />
          <app-system-summary-card
            [title]="allInAvailable() ? 'Avg Cost/User (all-in)' : 'Avg Cost/User'"
            [value]="formattedAvgCostPerUser()"
            [detail]="avgCostPerUserDetail()"
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

        <!-- Tab strip. Raised-pill idiom per app conventions, not an
             underline: the brand token has no dark variant, so a brand-
             coloured underline all but disappears on the dark surface.
             On small screens the strip collapses to a select — four
             labelled tabs with icons do not fit a phone without wrapping
             to two rows. -->
        <div class="mt-8">
          <div class="relative grid grid-cols-1 sm:hidden">
            <select
              [value]="activeTab()"
              (change)="selectTab($any($event.target).value)"
              aria-label="Select a view"
              class="col-start-1 row-start-1 w-full appearance-none rounded-2xl border border-gray-300 bg-white py-2 pl-3 pr-9 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
            >
              @for (tab of tabs; track tab.id) {
                <option [value]="tab.id">{{ tab.label }}</option>
              }
            </select>
            <ng-icon
              name="heroChevronDown"
              class="pointer-events-none col-start-1 row-start-1 mr-3 size-4 self-center justify-self-end text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
          </div>

          <!-- inline-flex, never flex: a strip stretched to the content
               width reads as a segmented control over the whole page rather
               than as four choices. -->
          <div
            role="tablist"
            aria-label="Cost analytics views"
            (keydown)="onTabKeydown($event)"
            class="hidden items-center gap-1 rounded-2xl border border-gray-200 bg-gray-50 p-1 sm:inline-flex dark:border-gray-700 dark:bg-gray-800"
          >
            @for (tab of tabs; track tab.id) {
              <button
                type="button"
                role="tab"
                [id]="'cost-tab-' + tab.id"
                [attr.aria-selected]="activeTab() === tab.id"
                [attr.aria-controls]="'cost-panel-' + tab.id"
                [tabindex]="activeTab() === tab.id ? 0 : -1"
                (click)="selectTab(tab.id)"
                class="inline-flex items-center gap-2 rounded-xl px-4 py-1.5 text-sm/6 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="
                  activeTab() === tab.id
                    ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                    : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'
                "
              >
                <ng-icon [name]="tab.icon" class="size-4 shrink-0" aria-hidden="true" />
                <span>{{ tab.label }}</span>
                @if (tabCounts()[tab.id] !== null) {
                  <span
                    class="hidden rounded-full px-2 py-0.5 text-xs/5 font-medium tabular-nums md:inline-block"
                    [class]="
                      activeTab() === tab.id
                        ? 'bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50'
                        : 'bg-gray-200/70 text-gray-600 dark:bg-gray-700/60 dark:text-gray-400'
                    "
                  >
                    {{ tabCounts()[tab.id] }}
                  </span>
                }
              </button>
            }
          </div>
        </div>

        <!-- Tab panels -->
        <div class="mt-6">
          @switch (activeTab()) {
            @case ('models') {
              <section
                role="tabpanel"
                id="cost-panel-models"
                aria-labelledby="cost-tab-models"
                tabindex="0"
              >
                <app-model-breakdown [data]="modelUsage()" />
              </section>
            }
            @case ('trends') {
              <section
                role="tabpanel"
                id="cost-panel-trends"
                aria-labelledby="cost-tab-trends"
                tabindex="0"
              >
                <app-cost-trends-chart [data]="trends()" />
              </section>
            }
            @case ('platform') {
              <section
                role="tabpanel"
                id="cost-panel-platform"
                aria-labelledby="cost-tab-platform"
                tabindex="0"
              >
                <app-platform-cost-breakdown [summary]="platformCosts()" />
              </section>
            }
            @case ('users') {
              <section
                role="tabpanel"
                id="cost-panel-users"
                aria-labelledby="cost-tab-users"
                tabindex="0"
              >
                <app-top-users-table
                  [users]="topUsers()"
                  [loading]="loadingTopUsers()"
                  [hasMore]="hasMoreUsers()"
                  (userClick)="onUserClick($event)"
                  (loadMore)="onLoadMoreUsers()"
                />
              </section>
            }
            @case ('conversations') {
              <section
                role="tabpanel"
                id="cost-panel-conversations"
                aria-labelledby="cost-tab-conversations"
                tabindex="0"
                class="space-y-6"
              >
                <!-- Session cost anatomy lookup. Lives with the conversation
                     list rather than in the page header: it is the same
                     drill-down the rows below link to, reached by ID instead
                     of by cost rank. -->
                <form
                  (ngSubmit)="onInspectSession()"
                  class="flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-center sm:justify-between dark:border-gray-700 dark:bg-gray-800"
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

                <app-top-sessions-table
                  [sessions]="topSessions()"
                  [loading]="loadingTopSessions()"
                  [truncated]="topSessionsTruncated()"
                  [hasLoaded]="topSessionsLoaded()"
                  (load)="onLoadTopSessions()"
                  (sessionClick)="onSessionClick($event)"
                />
              </section>
            }
          }
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
  platformCosts = this.stateService.platformCosts;

  /**
   * Tab order, and the default.
   *
   * Model Usage leads because the donut answers the first question an admin
   * brings to this page — *what* is the money going to — and it is the only
   * panel that is complete on load with no further fetch or scan.
   */
  protected readonly tabs: ReadonlyArray<{
    id: CostTab;
    label: string;
    icon: string;
  }> = [
    { id: 'models', label: 'Model Usage', icon: 'heroChartPie' },
    { id: 'trends', label: 'Cost Trends', icon: 'heroArrowTrendingUp' },
    { id: 'platform', label: 'Platform', icon: 'heroServerStack' },
    { id: 'users', label: 'Top Users', icon: 'heroUsers' },
    { id: 'conversations', label: 'Conversations', icon: 'heroChatBubbleLeftRight' },
  ];

  protected readonly activeTab = signal<CostTab>('models');

  /**
   * Row counts for the tab badges.
   *
   * `null` means "nothing to count yet" and hides the badge rather than
   * showing a zero: the conversations scan is on demand, and a `0` beside it
   * would read as "no expensive conversations" when it means "not scanned".
   */
  protected readonly tabCounts = computed<Record<CostTab, number | null>>(() => ({
    models: this.modelUsage().length || null,
    trends: this.trends().length || null,
    // Service count, not a dollar figure: the badge is a row count
    // everywhere else on the strip, and a "$686" badge beside "20" and "11"
    // would read as three of the same quantity.
    platform: this.platformServiceCount() || null,
    users: this.topUsers().length || null,
    conversations: this.topSessions().length || null,
  }));

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

  protected readonly platformServiceCount = computed(
    () =>
      (this.platformCosts()?.services ?? []).filter(s => s.category === 'platform')
        .length,
  );

  /**
   * Whether the headline cards can report all-in cost.
   *
   * False in an environment where the daily Cost Explorer sync is off (it is
   * opt-in), in which case the cards keep their original inference-only
   * meaning AND their original titles. The title carries the basis on
   * purpose: a figure that silently changes what it measures is one two
   * people will quote differently from the same screen.
   */
  protected readonly allInAvailable = computed(
    () => this.platformCosts()?.available === true,
  );

  // Formatted values for display
  formattedTotalCost = computed(() => {
    const platform = this.platformCosts();
    if (platform?.available) return this.formatCurrency(platform.totalCost);
    return this.formatCurrency(this.stateService.totalCost());
  });

  protected readonly totalCostDetail = computed(() => {
    const platform = this.platformCosts();
    if (!platform?.available) return null;
    return (
      `${this.formatCurrency(platform.inferenceCost)} inference + ` +
      `${this.formatCurrency(platform.platformCost)} platform`
    );
  });

  formattedAvgCostPerUser = computed(() => {
    const platform = this.platformCosts();
    if (platform?.available) return this.formatCurrency(platform.costPerUser);

    const cost = this.stateService.totalCost();
    const users = this.stateService.activeUsers();
    if (users === 0) return this.formatCurrency(0);
    return this.formatCurrency(cost / users);
  });

  protected readonly avgCostPerUserDetail = computed(() => {
    const platform = this.platformCosts();
    if (!platform?.available) return null;
    return (
      `${this.formatCurrency(platform.inferenceCostPerUser)} inference + ` +
      `${this.formatCurrency(platform.platformCostPerUser)} platform`
    );
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

    // Awaited separately and never allowed to throw: the platform figures
    // enrich a page that works without them, so an environment with the sync
    // disabled must not lose the other four tabs. `loadPlatformCosts`
    // swallows its own failures for the same reason.
    await this.stateService.loadPlatformCosts();
  }

  protected selectTab(tab: CostTab): void {
    this.activeTab.set(tab);
  }

  /**
   * Roving-focus arrow keys across the strip.
   *
   * Only the selected tab is in the tab order (`tabindex` above), which is
   * the correct tab pattern but leaves the other three unreachable from the
   * keyboard without this.
   */
  protected onTabKeydown(event: KeyboardEvent): void {
    const delta =
      event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (delta === 0) return;

    event.preventDefault();

    const current = this.tabs.findIndex(t => t.id === this.activeTab());
    const next = this.tabs[(current + delta + this.tabs.length) % this.tabs.length];
    this.activeTab.set(next.id);

    // Follow focus, so the next arrow press moves from where the user is.
    document.getElementById(`cost-tab-${next.id}`)?.focus();
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
