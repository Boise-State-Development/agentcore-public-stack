import {
  Component,
  ChangeDetectionStrategy,
  input,
  output,
  computed,
  signal,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { TopUserCost } from '../models';

type SortColumn = 'cost' | 'requests' | 'avgCost' | 'quota';
type SortDirection = 'asc' | 'desc';

/**
 * Top users table component for admin cost dashboard.
 * Displays a sortable table of users ranked by cost with pagination support.
 */
@Component({
  selector: 'app-top-users-table',
  imports: [DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="bg-white dark:bg-gray-800 rounded-lg shadow-xs border border-gray-200 dark:border-gray-700"
    >
      <!-- Header -->
      <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-white">
          Top Users by Cost
        </h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {{ users().length }} users loaded
        </p>
      </div>

      <!-- Table -->
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
          <thead class="bg-gray-50 dark:bg-gray-900">
            <tr>
              <th
                class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
              >
                Rank
              </th>
              <th
                class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
              >
                User
              </th>
              <th
                class="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
                [class.text-blue-600]="sortColumn() === 'cost'"
                [class.dark:text-blue-400]="sortColumn() === 'cost'"
                [class.text-gray-500]="sortColumn() !== 'cost'"
                [class.dark:text-gray-400]="sortColumn() !== 'cost'"
                (click)="toggleSort('cost')"
              >
                <span class="inline-flex items-center gap-1">
                  Total Cost
                  @if (sortColumn() === 'cost') {
                    <svg class="size-4" viewBox="0 0 20 20" fill="currentColor">
                      @if (sortDirection() === 'desc') {
                        <path
                          fill-rule="evenodd"
                          d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                          clip-rule="evenodd"
                        />
                      } @else {
                        <path
                          fill-rule="evenodd"
                          d="M14.77 12.79a.75.75 0 01-1.06-.02L10 8.832 6.29 12.77a.75.75 0 11-1.08-1.04l4.25-4.5a.75.75 0 011.08 0l4.25 4.5a.75.75 0 01-.02 1.06z"
                          clip-rule="evenodd"
                        />
                      }
                    </svg>
                  }
                </span>
              </th>
              <th
                class="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
                [class.text-blue-600]="sortColumn() === 'requests'"
                [class.dark:text-blue-400]="sortColumn() === 'requests'"
                [class.text-gray-500]="sortColumn() !== 'requests'"
                [class.dark:text-gray-400]="sortColumn() !== 'requests'"
                (click)="toggleSort('requests')"
              >
                <span class="inline-flex items-center gap-1">
                  Requests
                  @if (sortColumn() === 'requests') {
                    <svg class="size-4" viewBox="0 0 20 20" fill="currentColor">
                      @if (sortDirection() === 'desc') {
                        <path
                          fill-rule="evenodd"
                          d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                          clip-rule="evenodd"
                        />
                      } @else {
                        <path
                          fill-rule="evenodd"
                          d="M14.77 12.79a.75.75 0 01-1.06-.02L10 8.832 6.29 12.77a.75.75 0 11-1.08-1.04l4.25-4.5a.75.75 0 011.08 0l4.25 4.5a.75.75 0 01-.02 1.06z"
                          clip-rule="evenodd"
                        />
                      }
                    </svg>
                  }
                </span>
              </th>
              <th
                class="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
                [class.text-blue-600]="sortColumn() === 'avgCost'"
                [class.dark:text-blue-400]="sortColumn() === 'avgCost'"
                [class.text-gray-500]="sortColumn() !== 'avgCost'"
                [class.dark:text-gray-400]="sortColumn() !== 'avgCost'"
                (click)="toggleSort('avgCost')"
              >
                <span class="inline-flex items-center gap-1">
                  Avg/Request
                  @if (sortColumn() === 'avgCost') {
                    <svg class="size-4" viewBox="0 0 20 20" fill="currentColor">
                      @if (sortDirection() === 'desc') {
                        <path
                          fill-rule="evenodd"
                          d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                          clip-rule="evenodd"
                        />
                      } @else {
                        <path
                          fill-rule="evenodd"
                          d="M14.77 12.79a.75.75 0 01-1.06-.02L10 8.832 6.29 12.77a.75.75 0 11-1.08-1.04l4.25-4.5a.75.75 0 011.08 0l4.25 4.5a.75.75 0 01-.02 1.06z"
                          clip-rule="evenodd"
                        />
                      }
                    </svg>
                  }
                </span>
              </th>
              <th
                class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
              >
                Tier
              </th>
              <th
                class="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
                [class.text-blue-600]="sortColumn() === 'quota'"
                [class.dark:text-blue-400]="sortColumn() === 'quota'"
                [class.text-gray-500]="sortColumn() !== 'quota'"
                [class.dark:text-gray-400]="sortColumn() !== 'quota'"
                (click)="toggleSort('quota')"
              >
                <span class="inline-flex items-center gap-1">
                  Quota Used
                  @if (sortColumn() === 'quota') {
                    <svg class="size-4" viewBox="0 0 20 20" fill="currentColor">
                      @if (sortDirection() === 'desc') {
                        <path
                          fill-rule="evenodd"
                          d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                          clip-rule="evenodd"
                        />
                      } @else {
                        <path
                          fill-rule="evenodd"
                          d="M14.77 12.79a.75.75 0 01-1.06-.02L10 8.832 6.29 12.77a.75.75 0 11-1.08-1.04l4.25-4.5a.75.75 0 011.08 0l4.25 4.5a.75.75 0 01-.02 1.06z"
                          clip-rule="evenodd"
                        />
                      }
                    </svg>
                  }
                </span>
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
            @for (user of sortedUsers(); track user.userId; let i = $index) {
              <tr
                class="hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer transition-colors"
                (click)="userClick.emit(user.userId)"
              >
                <!-- Rank -->
                <td
                  class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400 whitespace-nowrap"
                >
                  {{ i + 1 }}
                </td>

                <!-- User -->
                <td class="px-4 py-3 whitespace-nowrap">
                  <div class="flex items-center gap-3">
                    <div
                      class="size-8 rounded-full bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center shrink-0"
                    >
                      <span
                        class="text-sm font-medium text-blue-600 dark:text-blue-400"
                      >
                        {{ getAvatarInitial(user) }}
                      </span>
                    </div>
                    <div class="min-w-0">
                      <p
                        class="text-sm font-medium text-gray-900 dark:text-white truncate"
                      >
                        {{ user.email || user.userId }}
                      </p>
                      @if (user.email) {
                        <p
                          class="text-xs text-gray-500 dark:text-gray-400 truncate"
                        >
                          {{ user.userId }}
                        </p>
                      }
                    </div>
                  </div>
                </td>

                <!-- Total Cost -->
                <td
                  class="px-4 py-3 text-sm text-right font-medium text-gray-900 dark:text-white whitespace-nowrap"
                >
                  {{ formatCurrency(user.totalCost) }}
                </td>

                <!-- Requests -->
                <td
                  class="px-4 py-3 text-sm text-right text-gray-500 dark:text-gray-400 whitespace-nowrap"
                >
                  {{ formatNumber(user.totalRequests) }}
                </td>

                <!-- Avg Cost per Request -->
                <td
                  class="px-4 py-3 text-sm text-right text-gray-500 dark:text-gray-400 whitespace-nowrap"
                >
                  {{ formatCurrency(getAvgCostPerRequest(user)) }}
                </td>

                <!-- Tier Badge -->
                <td class="px-4 py-3 whitespace-nowrap">
                  @if (user.tierName) {
                    <span
                      class="inline-flex items-center px-2 py-1 rounded-full text-xs font-medium bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400"
                    >
                      {{ user.tierName }}
                    </span>
                  } @else {
                    <span class="text-sm text-gray-400 dark:text-gray-500">
                      —
                    </span>
                  }
                </td>

                <!-- Quota Used -->
                <td class="px-4 py-3 whitespace-nowrap">
                  @if (
                    user.quotaPercentage !== null &&
                    user.quotaPercentage !== undefined
                  ) {
                    <div class="flex items-center justify-end gap-2">
                      <div
                        class="w-24 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden"
                      >
                        <div
                          class="h-full rounded-full transition-all"
                          [class]="getQuotaBarClass(user.quotaPercentage)"
                          [style.width.%]="getQuotaWidth(user.quotaPercentage)"
                        ></div>
                      </div>
                      <span
                        class="text-xs w-12 text-right"
                        [class]="getQuotaTextClass(user.quotaPercentage)"
                      >
                        {{ user.quotaPercentage | number : '1.0-0' }}%
                      </span>
                    </div>
                  } @else {
                    <span
                      class="text-sm text-gray-400 dark:text-gray-500 text-right block"
                    >
                      —
                    </span>
                  }
                </td>
              </tr>
            } @empty {
              <tr>
                <td
                  colspan="7"
                  class="px-4 py-12 text-center text-sm text-gray-500 dark:text-gray-400"
                >
                  No users found for this period
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>

      <!-- Footer with Load More -->
      @if (loading()) {
        <div
          class="px-6 py-4 border-t border-gray-200 dark:border-gray-700 text-center"
        >
          <div class="inline-flex items-center gap-2 text-sm text-gray-500">
            <div
              class="animate-spin rounded-full size-4 border-2 border-gray-300 dark:border-gray-600 border-t-blue-600 dark:border-t-blue-400"
            ></div>
            Loading more users...
          </div>
        </div>
      } @else if (hasMore()) {
        <div
          class="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between"
        >
          <span class="text-sm text-gray-500 dark:text-gray-400">
            Showing {{ users().length }} users
          </span>
          <button
            type="button"
            (click)="loadMore.emit()"
            class="text-sm font-medium text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300 transition-colors"
          >
            Load more users
          </button>
        </div>
      } @else if (users().length > 0) {
        <div
          class="px-6 py-4 border-t border-gray-200 dark:border-gray-700 text-center"
        >
          <span class="text-sm text-gray-500 dark:text-gray-400">
            All {{ users().length }} users loaded
          </span>
        </div>
      }
    </div>
  `,
})
export class TopUsersTableComponent {
  // Inputs
  users = input.required<TopUserCost[]>();
  loading = input(false);
  hasMore = input(true);

  // Outputs
  userClick = output<string>();
  loadMore = output<void>();

  // Local state for sorting
  sortColumn = signal<SortColumn>('cost');
  sortDirection = signal<SortDirection>('desc');

  // Computed sorted users
  sortedUsers = computed(() => {
    const users = [...this.users()];
    const column = this.sortColumn();
    const direction = this.sortDirection();

    return users.sort((a, b) => {
      let valueA: number;
      let valueB: number;

      switch (column) {
        case 'cost':
          valueA = a.totalCost;
          valueB = b.totalCost;
          break;
        case 'requests':
          valueA = a.totalRequests;
          valueB = b.totalRequests;
          break;
        case 'avgCost':
          valueA = this.getAvgCostPerRequest(a);
          valueB = this.getAvgCostPerRequest(b);
          break;
        case 'quota':
          valueA = a.quotaPercentage ?? -1;
          valueB = b.quotaPercentage ?? -1;
          break;
        default:
          return 0;
      }

      const comparison = valueA - valueB;
      return direction === 'desc' ? -comparison : comparison;
    });
  });

  toggleSort(column: SortColumn): void {
    if (this.sortColumn() === column) {
      this.sortDirection.update(dir => (dir === 'desc' ? 'asc' : 'desc'));
    } else {
      this.sortColumn.set(column);
      this.sortDirection.set('desc');
    }
  }

  getAvatarInitial(user: TopUserCost): string {
    if (user.email) {
      return user.email.charAt(0).toUpperCase();
    }
    return user.userId.charAt(0).toUpperCase();
  }

  getAvgCostPerRequest(user: TopUserCost): number {
    if (!user.totalRequests || user.totalRequests === 0) {
      return 0;
    }
    return user.totalCost / user.totalRequests;
  }

  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  }

  formatNumber(value: number): string {
    return new Intl.NumberFormat('en-US').format(value);
  }

  getQuotaWidth(percentage: number): number {
    return Math.min(percentage, 100);
  }

  getQuotaBarClass(percentage: number): string {
    if (percentage >= 100) return 'bg-red-500';
    if (percentage >= 80) return 'bg-yellow-500';
    return 'bg-green-500';
  }

  getQuotaTextClass(percentage: number): string {
    if (percentage >= 100) {
      return 'text-red-600 dark:text-red-400 font-medium';
    }
    if (percentage >= 80) {
      return 'text-yellow-600 dark:text-yellow-400';
    }
    return 'text-gray-500 dark:text-gray-400';
  }
}
