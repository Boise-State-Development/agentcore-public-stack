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
      <div class="flex items-center justify-between">
        <div class="flex-1">
          <p class="text-sm font-medium text-gray-500 dark:text-gray-400">
            {{ title() }}
          </p>
          <p class="mt-2 text-3xl font-semibold text-gray-900 dark:text-white">
            {{ value() }}
          </p>

          @if (trend() !== null && trend() !== undefined) {
            <div class="mt-2 flex items-center gap-1">
              @if (trend()! > 0) {
                <ng-icon
                  name="heroArrowTrendingUp"
                  class="size-4 text-green-500"
                />
                <span class="text-sm text-green-600 dark:text-green-400">
                  +{{ trend() | number : '1.1-1' }}%
                </span>
              } @else if (trend()! < 0) {
                <ng-icon
                  name="heroArrowTrendingDown"
                  class="size-4 text-red-500"
                />
                <span class="text-sm text-red-600 dark:text-red-400">
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

        <div
          class="flex size-12 items-center justify-center rounded-lg"
          [class]="iconBackgroundClass()"
        >
          <ng-icon [name]="icon()" class="size-6" [class]="iconColorClass()" />
        </div>
      </div>
    </div>
  `,
})
export class SystemSummaryCardComponent {
  title = input.required<string>();
  value = input.required<string>();
  trend = input<number | null>(null);
  icon = input<SummaryCardIcon>('heroCurrencyDollar');

  iconBackgroundClass = computed(() => {
    const iconName = this.icon();
    switch (iconName) {
      case 'heroCurrencyDollar':
        return 'bg-green-100 dark:bg-green-900/30';
      case 'heroChartBar':
        return 'bg-blue-100 dark:bg-blue-900/30';
      case 'heroUsers':
        return 'bg-purple-100 dark:bg-purple-900/30';
      case 'heroBolt':
        return 'bg-amber-100 dark:bg-amber-900/30';
      case 'heroUserCircle':
        return 'bg-indigo-100 dark:bg-indigo-900/30';
      default:
        return 'bg-gray-100 dark:bg-gray-900/30';
    }
  });

  iconColorClass = computed(() => {
    const iconName = this.icon();
    switch (iconName) {
      case 'heroCurrencyDollar':
        return 'text-green-600 dark:text-green-400';
      case 'heroChartBar':
        return 'text-blue-600 dark:text-blue-400';
      case 'heroUsers':
        return 'text-purple-600 dark:text-purple-400';
      case 'heroBolt':
        return 'text-amber-600 dark:text-amber-400';
      case 'heroUserCircle':
        return 'text-indigo-600 dark:text-indigo-400';
      default:
        return 'text-gray-600 dark:text-gray-400';
    }
  });
}
