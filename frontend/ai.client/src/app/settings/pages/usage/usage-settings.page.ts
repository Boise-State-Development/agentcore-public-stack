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
import { QuotaStatusService } from '../../../services/quota/quota-status.service';
import { QuotaRequestService } from '../../../services/quota/quota-request.service';
import { QuotaRequest, toAmount } from '../../../services/quota/quota-request.model';
import { formatAmount, formatQuotaDate, formatUsd, perPeriod } from '../../../services/quota/quota-format';
import { FEATURES } from '../../../services/features';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { QuotaRequestActionComponent } from '../../../components/quota-request/quota-request-action.component';

const REQUEST_STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  info_requested: 'Funding source requested',
  approved: 'Approved',
  denied: 'Denied',
  withdrawn: 'Withdrawn',
};

const REQUEST_STATUS_CLASSES: Record<string, string> = {
  pending: 'bg-state-warning-50 text-state-warning-800 ring-state-warning-600/20 dark:bg-state-warning-900/30 dark:text-state-warning-200',
  info_requested: 'bg-state-warning-50 text-state-warning-800 ring-state-warning-600/20 dark:bg-state-warning-900/30 dark:text-state-warning-200',
  approved: 'bg-state-success-50 text-state-success-800 ring-state-success-600/20 dark:bg-state-success-900/30 dark:text-state-success-200',
  denied: 'bg-state-danger-50 text-state-danger-800 ring-state-danger-600/20 dark:bg-state-danger-900/30 dark:text-state-danger-200',
  withdrawn: 'bg-gray-50 text-gray-700 ring-gray-500/20 dark:bg-white/5 dark:text-gray-300',
};

@Component({
  selector: 'app-usage-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe, SpinnerComponent, QuotaRequestActionComponent],
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
            ? 'bg-primary-accessible text-white border-primary-accessible'
            : 'bg-white text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-600'"
          class="rounded-2xl border px-3 py-1.5 text-sm/6 font-medium transition-colors"
        >
          Current Month
        </button>
        <button
          type="button"
          (click)="loadLast30Days()"
          [class]="selectedPeriodType() === 'last30'
            ? 'bg-primary-accessible text-white border-primary-accessible'
            : 'bg-white text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-600'"
          class="rounded-2xl border px-3 py-1.5 text-sm/6 font-medium transition-colors"
        >
          Last 30 Days
        </button>

        <div class="h-5 w-px bg-gray-200 dark:bg-white/10"></div>

        <select
          (change)="loadMonth($any($event.target).value)"
          [value]="selectedPeriodType() === 'month' ? selectedMonthValue() : ''"
          class="rounded-sm border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors focus:border-primary-500 focus:outline-hidden focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300"
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
            <app-spinner size="lg" label="Loading cost data" />
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading cost data...</p>
          </div>
        </div>
      }

      <!-- Error State -->
      @else if (selectedPeriodType() === 'current' && costSummary.error()) {
        <div class="rounded-lg bg-state-danger-50 p-4 dark:bg-state-danger-900/20">
          <p class="text-sm/6 font-medium text-state-danger-800 dark:text-state-danger-200">Error loading cost data</p>
        </div>
      }

      @else if (customReportError()) {
        <div class="rounded-lg bg-state-danger-50 p-4 dark:bg-state-danger-900/20">
          <p class="text-sm/6 text-state-danger-800 dark:text-state-danger-200">{{ customReportError() }}</p>
        </div>
      }

      <!-- Cost Summary Cards -->
      @else if (activeData()) {
        <!-- Quota progress bar: how this period's spend sits against the
             monthly quota. Shown for every period so users can compare past
             usage to their limit, not just the current month. -->
        @if (quotaBar(); as q) {
          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <div class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">
                Quota usage
                <span class="font-normal text-gray-500 dark:text-gray-400">· {{ periodLabel() }}</span>
              </h3>
              <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                <span class="font-semibold text-gray-900 dark:text-white">{{ formatCurrency(q.usage) }}</span>
                <span class="text-gray-500 dark:text-gray-400"> of </span>
                <span class="font-semibold text-gray-900 dark:text-white">{{ formatCurrency(q.limit) }}</span>
                <span class="text-gray-500 dark:text-gray-400"> {{ q.periodWord }} quota</span>
              </p>
            </div>
            <div
              class="relative mt-3 h-2.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-white/10"
              role="progressbar"
              [attr.aria-valuenow]="q.pct"
              aria-valuemin="0"
              aria-valuemax="100"
              [attr.aria-label]="'Quota usage: ' + q.pct + ' percent used'"
            >
              @if (quotaSplit(); as split) {
                <!-- The extension's share of the track, behind the fill: the bar is
                     base + extensions, and this shows where the base ends. -->
                <div
                  class="absolute inset-y-0 right-0 bg-primary-100 dark:bg-primary-400/30"
                  [style.left.%]="split.basePct"
                  aria-hidden="true"
                  data-testid="quota-extension-segment"
                ></div>
              }
              <div
                class="relative h-full rounded-full transition-[width] duration-500 ease-out"
                [class]="q.fillClass"
                [style.width.%]="q.pct"
              ></div>
              @if (quotaSplit(); as split) {
                <div
                  class="absolute inset-y-0 w-0.5 bg-white dark:bg-gray-800"
                  [style.left.%]="split.basePct"
                  aria-hidden="true"
                ></div>
              }
            </div>
            <div class="mt-2 flex flex-wrap items-center justify-between gap-x-4 text-xs">
              <span [class]="q.textClass">{{ q.pct | number: '1.0-1' }}% used</span>
              <span class="text-gray-500 dark:text-gray-400">
                {{ formatCurrency(q.remaining) }} remaining
                @if (quotaResetInfo()) {
                  · {{ quotaResetInfo() }}
                }
              </span>
            </div>
            @if (quotaSplit(); as split) {
              <p class="mt-2 text-xs/5 text-gray-700 dark:text-gray-300" data-testid="quota-split-legend">
                <span class="mr-1 inline-block size-2 rounded-full bg-gray-300 align-middle dark:bg-white/30" aria-hidden="true"></span>{{ split.base }}
                @for (part of split.parts; track $index) {
                  <span> + </span><span class="mr-1 inline-block size-2 rounded-full bg-primary-300 align-middle dark:bg-primary-400" aria-hidden="true"></span>{{ part }}
                }
              </p>
            }
            @if (standingTerms(); as terms) {
              <p class="mt-2 text-xs/5 text-gray-700 dark:text-gray-300">{{ terms }}</p>
            }
            @if (chargedLine(); as charged) {
              <p class="mt-1 text-xs/5 text-gray-700 dark:text-gray-300" data-testid="quota-charged">{{ charged }}</p>
            }
            @if (offerRequests()) {
              <div class="mt-4 flex flex-wrap items-center gap-2 border-t border-gray-100 pt-4 dark:border-white/10">
                <app-quota-request-action variant="button" />
              </div>
            }
          </div>
        } @else if (quotaUnlimited()) {
          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">Quota usage</h3>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              You have an unlimited quota — no spending limit applies to your account.
            </p>
          </div>
        }

        @if (offerRequests() && myRequests().length) {
          <section aria-labelledby="your-quota-requests" data-testid="your-quota-requests">
            <h3 id="your-quota-requests" class="mb-3 text-sm/6 font-medium text-gray-900 dark:text-white">Your requests</h3>
            <ul role="list" class="divide-y divide-gray-200 overflow-hidden rounded-lg border border-gray-200 bg-white dark:divide-white/10 dark:border-white/10 dark:bg-gray-800">
              @for (r of myRequests(); track r.requestId) {
                <li class="px-4 py-3">
                  <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                    <p class="text-sm/6 text-gray-900 dark:text-white">
                      <span class="font-medium">{{ requestAmount(r) }}</span>
                      <span class="text-gray-600 dark:text-gray-400"> · through {{ date(r.requestedUntil) }} · sent {{ date(r.createdAt) }}</span>
                    </p>
                    <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs/5 font-medium ring-1 ring-inset" [class]="statusClass(r)">{{ statusLabel(r) }}</span>
                  </div>
                  @if (decisionLine(r); as line) {
                    <p class="mt-1 text-xs/5 text-gray-700 dark:text-gray-300">{{ line }}</p>
                  }
                  @if (noteOf(r); as note) {
                    <p class="mt-1 text-xs/5 text-gray-700 italic dark:text-gray-300">“{{ note }}”</p>
                  }
                </li>
              }
            </ul>
          </section>
        } @else if (offerRequests() && requestsError()) {
          <p class="text-sm/6 text-state-danger-700 dark:text-state-danger-300">{{ requestsError() }}</p>
        }

        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div class="rounded-lg border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-800">
            <h3 class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Total Cost</h3>
            <p class="mt-2 text-2xl font-bold text-gray-900 dark:text-white">
              {{ formatCurrency(totalCost()) }}
            </p>
            @if (totalCacheSavings() > 0) {
              <p class="mt-1 text-xs text-state-success-700 dark:text-state-success-400">
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
  private quotaStatusService = inject(QuotaStatusService);
  private features = inject(FEATURES);
  private quotaRequests = this.features.quotaRequests ? inject(QuotaRequestService) : null;

  readonly costSummary = this.costService.currentMonthSummary;
  readonly quotaStatus = this.quotaStatusService.status;
  readonly customReportData = signal<UserCostSummary | null>(null);
  readonly selectedPeriodType = signal<'current' | 'last30' | 'month'>('current');
  readonly selectedMonthValue = signal('');
  readonly isLoadingCustomReport = signal(false);
  readonly customReportError = signal<string | null>(null);

  readonly previousMonths = this.buildPreviousMonths(11);

  constructor() {
    // quota-status reads fresh, so opening the page after a decision shows the new limit.
    if (this.quotaRequests) {
      this.quotaStatusService.reload();
      void this.quotaRequests.loadRequests();
    }
  }

  /** Requests are on, the current period is selected, and the user has a capped quota. */
  readonly offerRequests = computed(() => {
    if (!this.quotaRequests || this.selectedPeriodType() !== 'current') return false;
    const status = this.quotaStatus.value();
    return !!status?.configured && !status.unlimited;
  });

  readonly myRequests = computed<QuotaRequest[]>(() => this.quotaRequests?.requests() ?? []);
  readonly requestsError = computed(() => this.quotaRequests?.requestsLoadError() ?? null);

  /**
   * The bar as base + extensions (quota-requests spec §5.3), for the current
   * period: where the base ends on the track, and the legend
   * `$30 central + $20 additional through Nov 30`.
   */
  readonly quotaSplit = computed(() => {
    if (this.selectedPeriodType() !== 'current') return null;
    const status = this.quotaStatus.value();
    const extensions = status?.extensions ?? [];
    const base = status?.baseLimit;
    const limit = status?.monthlyLimit;
    if (!extensions.length || base == null || !limit || limit <= 0) return null;
    const parts = extensions.map(e => {
      const until = ` through ${formatQuotaDate(e.validUntil, false)}`;
      if (e.amount == null) return `usage charged to ${e.fundingLabel || 'your fund'}, no cap${until}`;
      if (e.fundingMode === 'chargeback') {
        return `${formatAmount(e.amount)} charged to ${e.fundingLabel || 'your fund'}${until}`;
      }
      return `${formatAmount(e.amount)} additional${until}`;
    });
    return {
      basePct: Math.min(100, Math.max(0, (base / limit) * 100)),
      base: `${formatAmount(base)} central`,
      parts,
    };
  });

  /** A standing arrangement, spelled out (§4.4). */
  readonly standingTerms = computed(() => {
    if (this.selectedPeriodType() !== 'current') return null;
    const status = this.quotaStatus.value();
    const standing = status?.extensions?.find(e => e.standing);
    if (!standing || status?.baseLimit == null) return null;
    const fund = standing.fundingLabel ? ` to ${standing.fundingLabel}` : '';
    const cap = standing.amount != null ? `, up to ${formatAmount(standing.amount)} ${perPeriod(status.periodType)}` : '';
    return `After your ${formatAmount(status.baseLimit)} included quota, usage is charged${fund}${cap}, through ${formatQuotaDate(standing.validUntil)}.`;
  });

  /** What has been charged to a fund this period. */
  readonly chargedLine = computed(() => {
    if (this.selectedPeriodType() !== 'current') return null;
    const status = this.quotaStatus.value();
    const charged = status?.charged;
    if (!charged) return null;
    const period = status?.periodType === 'daily' ? 'today' : 'this month';
    const cap = charged.cap != null ? ` of ${formatUsd(charged.cap)}` : '';
    return `Charged to ${charged.label} ${period}: ${formatUsd(charged.thisPeriod)}${cap}`;
  });

  requestAmount(r: QuotaRequest): string {
    const amount = formatAmount(toAmount(r.requestedAmount));
    const per = perPeriod(this.quotaStatus.value()?.periodType);
    return r.kind === 'standing' ? `Up to ${amount} ${per}, ongoing` : `${amount} ${per}`;
  }

  /**
   * An approval for a different amount says so plainly (§5.3). One whose
   * extension an admin later revoked says when it ended instead of the date
   * it was granted through, which would otherwise read as still in force.
   */
  decisionLine(r: QuotaRequest): string | null {
    const decision = r.decision;
    if (r.status !== 'approved' || !decision) return null;
    const granted = toAmount(decision.amount);
    const asked = toAmount(r.requestedAmount);
    const ended = decision.extensionRevokedAt ? formatQuotaDate(decision.extensionRevokedAt) : '';
    const until = !ended && decision.validUntil ? ` through ${formatQuotaDate(decision.validUntil)}` : '';
    const afterwards = ended ? ` It was ended early on ${ended}.` : '';
    if (Number.isFinite(granted) && granted !== asked) {
      return `You asked for ${formatAmount(asked)}; ${formatAmount(granted)} was approved${until}.${afterwards}`;
    }
    if (ended) return `Approved, then ended early on ${ended}.`;
    return until ? `Approved${until}.` : null;
  }

  noteOf(r: QuotaRequest): string | null {
    if (r.status === 'info_requested') return r.adminNote || null;
    return r.decision?.note || null;
  }

  statusLabel(r: QuotaRequest): string {
    return REQUEST_STATUS_LABELS[r.status] ?? r.status;
  }

  statusClass(r: QuotaRequest): string {
    return REQUEST_STATUS_CLASSES[r.status] ?? REQUEST_STATUS_CLASSES['withdrawn'];
  }

  date(value: string): string {
    return formatQuotaDate(value);
  }

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

  /**
   * Quota progress bar for the selected period, or null when there is nothing
   * to show a bar against (quota still loading, unconfigured, unlimited, or a
   * zero limit). Numerator is the *selected period's* spend; denominator is
   * the resolved monthly (or daily) limit — so a past month is compared to the
   * user's current quota, which is what they asked to see.
   */
  readonly quotaBar = computed(() => {
    const status = this.quotaStatus.value();
    if (!status || !status.configured || status.unlimited) return null;
    const limit = status.monthlyLimit;
    if (!limit || limit <= 0) return null;

    const usage = this.totalCost();
    const pct = Math.min(100, Math.max(0, (usage / limit) * 100));
    const remaining = Math.max(0, limit - usage);
    const periodWord = status.periodType === 'daily' ? 'daily' : 'monthly';

    let fillClass = 'bg-state-success-500 dark:bg-state-success-400';
    let textClass = 'text-gray-500 dark:text-gray-400';
    if (pct >= 90) {
      fillClass = 'bg-state-danger-500 dark:bg-state-danger-400';
      textClass = 'text-state-danger-600 dark:text-state-danger-400 font-medium';
    } else if (pct >= 75) {
      fillClass = 'bg-state-warning-500 dark:bg-state-warning-400';
      textClass = 'text-state-warning-600 dark:text-state-warning-400 font-medium';
    }

    return { usage, limit, pct, remaining, periodWord, fillClass, textClass };
  });

  /** True when the user is on an unlimited tier/override. */
  readonly quotaUnlimited = computed(() => {
    const status = this.quotaStatus.value();
    return !!status && status.configured && status.unlimited;
  });

  /**
   * Reset note ("Quota resets in N day(s)") — only meaningful for the current
   * period, since the backend figure is relative to now.
   */
  readonly quotaResetInfo = computed(() => {
    if (this.selectedPeriodType() !== 'current') return null;
    return this.quotaStatus.value()?.resetInfo ?? null;
  });

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
    this.quotaStatusService.reload();
    if (this.quotaRequests) void this.quotaRequests.loadRequests();
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
