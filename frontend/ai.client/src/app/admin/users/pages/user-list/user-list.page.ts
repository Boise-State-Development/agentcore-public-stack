import {
  Component,
  ChangeDetectionStrategy,
  inject,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroMagnifyingGlass,
  heroUser,
  heroChevronRight,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { UserStateService } from '../../services/user-state.service';
import { UserListItem, UserStatus } from '../../models';

@Component({
  selector: 'app-user-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [
    provideIcons({ heroMagnifyingGlass, heroUser, heroChevronRight, heroXMark }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <div class="mb-6">
      <h1 class="text-2xl/9 font-bold mb-2">User Lookup</h1>
      <p class="text-gray-600 dark:text-gray-400">
        Search and browse users to view their profile, costs, and quota status.
      </p>
    </div>

    <!-- Search Bar -->
    <div class="mb-6">
      <div class="relative">
        <ng-icon
          name="heroMagnifyingGlass"
          class="absolute left-3 top-1/2 -translate-y-1/2 size-5 text-gray-400"
        />
        <input
          type="email"
          [(ngModel)]="searchEmail"
          (keyup.enter)="search()"
          placeholder="Search by email address..."
          class="w-full pl-10 pr-10 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
        />
        @if (searchEmail) {
          <button
            (click)="clearSearch()"
            class="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
          >
            <ng-icon name="heroXMark" class="size-5" />
          </button>
        }
      </div>
    </div>

    <!-- Filters -->
    <div class="flex gap-4 mb-6">
      <select
        [ngModel]="state.statusFilter()"
        (ngModelChange)="onStatusChange($event)"
        class="px-3 py-2 border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-600"
      >
        <option value="active">Active Users</option>
        <option value="inactive">Inactive Users</option>
        <option value="suspended">Suspended Users</option>
      </select>
    </div>

    <!-- Error State -->
    @if (state.hasError()) {
      <div class="mb-6 p-4 bg-red-50 border border-red-200 rounded-sm text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-200">
        <p>{{ state.error() }}</p>
        <button
          (click)="state.clearError()"
          class="mt-2 text-sm underline hover:no-underline"
        >
          Dismiss
        </button>
      </div>
    }

    <!-- Loading State -->
    @if (state.loading() && state.users().length === 0) {
      <div class="flex items-center justify-center h-64">
        <div class="flex flex-col items-center gap-4">
          <div
            class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"
          ></div>
          <p class="text-sm text-gray-500 dark:text-gray-400">
            Loading users...
          </p>
        </div>
      </div>
    }

    <!-- User List -->
    <div class="space-y-2">
      @for (user of state.users(); track user.userId) {
        <div
          (click)="viewUser(user)"
          (keydown.enter)="viewUser(user)"
          tabindex="0"
          role="button"
          class="flex items-center gap-4 p-4 border border-gray-200 rounded-sm cursor-pointer hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800 transition-colors"
        >
          <!-- Avatar -->
          <div
            class="flex items-center justify-center size-10 rounded-full bg-gray-200 dark:bg-gray-700 shrink-0"
          >
            <ng-icon name="heroUser" class="size-5 text-gray-500" />
          </div>

          <!-- User Info -->
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2">
              <span class="font-medium truncate">{{ user.email }}</span>
              @if (user.status !== 'active') {
                <span
                  class="px-2 py-0.5 text-xs rounded-xs shrink-0"
                  [class]="getStatusClass(user.status)"
                >
                  {{ user.status }}
                </span>
              }
            </div>
            <div class="text-sm/6 text-gray-500 dark:text-gray-400">
              {{ user.name || 'No name' }} &middot; Last login:
              {{ formatDate(user.lastLoginAt) }}
            </div>
          </div>

          <!-- Quick Stats (if available) -->
          @if (user.quotaUsagePercentage !== undefined) {
            <div class="text-right shrink-0">
              <div class="text-sm/6 font-medium">
                {{ user.quotaUsagePercentage }}% quota used
              </div>
              @if (user.currentMonthCost != null) {
                <div class="text-sm/6 text-gray-500">
                  \${{ user.currentMonthCost.toFixed(2) }} this month
                </div>
              }
            </div>
          }

          <ng-icon name="heroChevronRight" class="size-5 text-gray-400 shrink-0" />
        </div>
      }
    </div>

    <!-- Empty State -->
    @if (state.users().length === 0 && !state.loading()) {
      <div class="text-center py-12 text-gray-500">
        <ng-icon name="heroUser" class="size-12 mx-auto mb-4 text-gray-300" />
        <p class="text-lg/7">No users found</p>
        <p class="text-sm/6">Try adjusting your search or filters</p>
      </div>
    }

    <!-- Load More -->
    @if (state.hasMore()) {
      <div class="mt-6 text-center">
        <button
          (click)="loadMore()"
          [disabled]="state.loading()"
          class="px-4 py-2 text-blue-600 hover:text-blue-800 disabled:opacity-50 dark:text-blue-400 dark:hover:text-blue-300"
        >
          @if (state.loading()) {
            Loading...
          } @else {
            Load More
          }
        </button>
      </div>
    }
  `,
})
export class UserListPage implements OnInit {
  state = inject(UserStateService);
  private router = inject(Router);

  searchEmail = '';

  ngOnInit(): void {
    this.state.loadUsers(true);
  }

  search(): void {
    if (this.searchEmail.trim()) {
      this.state.searchByEmail(this.searchEmail.trim());
    } else {
      this.state.loadUsers(true);
    }
  }

  clearSearch(): void {
    this.searchEmail = '';
    this.state.clearSearch();
  }

  onStatusChange(status: UserStatus): void {
    this.state.setStatusFilter(status);
  }

  viewUser(user: UserListItem): void {
    this.router.navigate(['/admin/users', user.userId]);
  }

  loadMore(): void {
    this.state.loadUsers(false);
  }

  formatDate(isoString: string): string {
    if (!isoString) {
      return 'Never';
    }
    const date = new Date(isoString);
    if (isNaN(date.getTime())) {
      return 'Never';
    }
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
      return 'Today';
    } else if (diffDays === 1) {
      return 'Yesterday';
    } else if (diffDays < 7) {
      return `${diffDays} days ago`;
    } else {
      return date.toLocaleDateString();
    }
  }

  getStatusClass(status: UserStatus): string {
    switch (status) {
      case 'suspended':
        return 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200';
      case 'inactive':
        return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200';
      default:
        return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200';
    }
  }
}
