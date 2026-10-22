import { Component, ChangeDetectionStrategy, input, computed } from '@angular/core';
import { FineTuningAccessResponse } from '../models/fine-tuning.models';

/**
 * Displays the user's monthly fine-tuning spend against their quota.
 *
 * The quota is denominated in dollars rather than GPU-hours: hours stopped
 * describing the budget once more than one instance type was offered.
 */
@Component({
  selector: 'app-quota-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="rounded-sm border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
      <div class="flex items-center justify-between">
        <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">Monthly Quota</h3>
        @if (access().quota_period; as period) {
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ period }}</span>
        }
      </div>
      <div class="mt-3">
        <div class="flex items-baseline justify-between">
          <span class="text-2xl font-bold text-gray-900 dark:text-white">
            \${{ usedUsd().toFixed(2) }}
          </span>
          <span class="text-sm/6 text-gray-500 dark:text-gray-400">
            / \${{ totalUsd().toFixed(2) }}
          </span>
        </div>
        <div
          class="mt-2 h-2 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"
          role="progressbar"
          [attr.aria-valuenow]="usedPercent()"
          aria-valuemin="0"
          aria-valuemax="100"
          [attr.aria-label]="'Quota usage: ' + usedPercent().toFixed(0) + '%'"
        >
          <div
            [class]="'h-full rounded-full transition-all ' + barColor()"
            [style.width.%]="usedPercent()"
          ></div>
        </div>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          \${{ remainingUsd().toFixed(2) }} remaining
        </p>
      </div>
    </div>
  `,
})
export class QuotaCardComponent {
  readonly access = input.required<FineTuningAccessResponse>();

  readonly usedUsd = computed(() => this.access().current_month_usage_usd ?? 0);
  readonly totalUsd = computed(() => this.access().monthly_quota_usd ?? 0);
  readonly remainingUsd = computed(() => Math.max(0, this.totalUsd() - this.usedUsd()));

  readonly usedPercent = computed(() => {
    const total = this.totalUsd();
    if (total <= 0) return 0;
    return Math.min(100, (this.usedUsd() / total) * 100);
  });

  readonly barColor = computed(() => {
    const pct = this.usedPercent();
    if (pct >= 90) return 'bg-state-danger-500';
    if (pct >= 70) return 'bg-state-warning-500';
    return 'bg-state-info-500';
  });
}
