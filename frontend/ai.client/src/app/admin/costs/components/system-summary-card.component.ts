import {
  Component,
  ChangeDetectionStrategy,
  input,
  computed,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCurrencyDollar,
  heroChartBar,
  heroUsers,
  heroBolt,
  heroArrowTrendingUp,
  heroArrowTrendingDown,
  heroUserCircle,
} from '@ng-icons/heroicons/outline';

export type SummaryCardIcon =
  | 'heroCurrencyDollar'
  | 'heroChartBar'
  | 'heroUsers'
  | 'heroBolt'
  | 'heroUserCircle';

/**
 * Summary card component for displaying a metric with title, value, optional trend, and icon.
 * Used in the admin cost dashboard for displaying key metrics.
 */
@Component({
  selector: 'app-system-summary-card',
  imports: [DecimalPipe, NgIcon],
  providers: [
    provideIcons({
      heroCurrencyDollar,
      heroChartBar,
      heroUsers,
      heroBolt,
      heroArrowTrendingUp,
      heroArrowTrendingDown,
      heroUserCircle,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="bg-white dark:bg-gray-800 rounded-lg shadow-xs border border-gray-200 dark:border-gray-700 p-6"
    >
      <div class="flex items-start justify-between gap-3">
        <p class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">
          {{ title() }}
        </p>
        <div
          class="flex size-8 shrink-0 items-center justify-center rounded-md"
          [class]="iconBackgroundClass()"
        >
          <ng-icon [name]="icon()" class="size-4" [class]="iconColorClass()" />
        </div>
      </div>

      <p class="mt-3 text-3xl/9 font-semibold text-gray-900 dark:text-white">
        {{ value() }}
      </p>

      @if (trend() !== null && trend() !== undefined) {
        <div class="mt-2 flex items-center gap-1">
          @if (trend()! > 0) {
            <ng-icon
              name="heroArrowTrendingUp"
              class="size-4 text-state-success-500"
            />
            <span class="text-sm text-state-success-600 dark:text-state-success-400">
              +{{ trend() | number : '1.1-1' }}%
            </span>
          } @else if (trend()! < 0) {
            <ng-icon
              name="heroArrowTrendingDown"
              class="size-4 text-state-danger-500"
            />
            <span class="text-sm text-state-danger-600 dark:text-state-danger-400">
              {{ trend() | number : '1.1-1' }}%
            </span>
          } @else {
            <span class="text-sm text-gray-500 dark:text-gray-400">
              No change
            </span>
          }
          <span class="text-sm text-gray-400 dark:text-gray-500">
            vs last period
          </span>
        </div>
      }
    </div>
  `,
})
export class SystemSummaryCardComponent {
  title = input.required<string>();
  value = input.required<string>();
  trend = input<number | null>(null);
  icon = input<SummaryCardIcon>('heroCurrencyDollar');

  // Decorative per-metric icon colors (Total Cost/Active Users/Cache
  // Savings/Avg Cost per User) — purely to keep the summary cards visually
  // distinguishable from each other, not a status or vendor identity. Uses
  // the meaning-agnostic accent tokens rather than state-* or vendor-*,
  // matching the memory-dashboard tag-palette precedent in identity.css.
  iconBackgroundClass = computed(() => {
    const iconName = this.icon();
    switch (iconName) {
      case 'heroCurrencyDollar':
        return 'bg-accent-3-100 dark:bg-accent-3-900/30';
      case 'heroChartBar':
        return 'bg-vendor-microsoft-100 dark:bg-vendor-microsoft-900/30';
      case 'heroUsers':
        return 'bg-accent-1-100 dark:bg-accent-1-900/30';
      case 'heroBolt':
        return 'bg-accent-4-100 dark:bg-accent-4-900/30';
      case 'heroUserCircle':
        return 'bg-accent-7-100 dark:bg-accent-7-900/30';
      default:
        return 'bg-gray-100 dark:bg-gray-900/30';
    }
  });

  iconColorClass = computed(() => {
    const iconName = this.icon();
    switch (iconName) {
      case 'heroCurrencyDollar':
        return 'text-accent-3-700 dark:text-accent-3-300';
      case 'heroChartBar':
        return 'text-vendor-microsoft-600 dark:text-vendor-microsoft-400';
      case 'heroUsers':
        return 'text-accent-1-700 dark:text-accent-1-300';
      case 'heroBolt':
        return 'text-accent-4-700 dark:text-accent-4-300';
      case 'heroUserCircle':
        return 'text-accent-7-700 dark:text-accent-7-300';
      default:
        return 'text-gray-600 dark:text-gray-400';
    }
  });
}
