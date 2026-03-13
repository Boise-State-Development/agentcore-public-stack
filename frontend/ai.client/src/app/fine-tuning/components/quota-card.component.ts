import { Component, ChangeDetectionStrategy, input, computed } from '@angular/core';
import { FineTuningAccessResponse } from '../models/fine-tuning.models';

/**
 * Displays the user's monthly fine-tuning quota usage as a card with a progress bar.
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
            {{ usedHours().toFixed(1) }}
          </span>
          <span class="text-sm/6 text-gray-500 dark:text-gray-400">
            / {{ totalHours().toFixed(0) }} hrs
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
          {{ remainingHours().toFixed(1) }} hours remaining
        </p>
      </div>
    </div>
  `,
})
export class QuotaCardComponent {
  readonly access = input.required<FineTuningAccessResponse>();

  readonly usedHours = computed(() => this.access().current_month_usage_hours ?? 0);
  readonly totalHours = computed(() => this.access().monthly_quota_hours ?? 0);
  readonly remainingHours = computed(() => Math.max(0, this.totalHours() - this.usedHours()));

  readonly usedPercent = computed(() => {
    const total = this.totalHours();
    if (total <= 0) return 0;
    return Math.min(100, (this.usedHours() / total) * 100);
  });

  readonly barColor = computed(() => {
    const pct = this.usedPercent();
    if (pct >= 90) return 'bg-red-500';
    if (pct >= 70) return 'bg-amber-500';
    return 'bg-blue-500';
  });
}
