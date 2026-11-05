import {
  Component,
  ChangeDetectionStrategy,
  DestroyRef,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Subject, debounceTime } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroMagnifyingGlass,
  heroUser,
  heroChevronRight,
  heroXMark,
  heroArrowLeft,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { UserStateService } from '../../services/user-state.service';
import { UserListItem, UserStatus, UserStatusFilter } from '../../models';
import { parseIso } from '../../../../utils/date';
import { SpinnerComponent } from '../../../../components/spinner/spinner.component';

/** Pause after the last keystroke before searching. */
export const SEARCH_DEBOUNCE_MS = 300;

const STATUS_LABELS: Record<UserStatus, string> = {
  active: 'Active',
  inactive: 'Inactive',
  suspended: 'Suspended',
};

@Component({
  selector: 'app-user-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon, SpinnerComponent],
  providers: [
    provideIcons({ heroMagnifyingGlass, heroUser, heroChevronRight, heroXMark, heroArrowLeft, heroExclamationTriangle }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <div class="mb-6">
      <h1 class="text-3xl/9 font-bold mb-2">User Lookup</h1>
      <p class="text-gray-600 dark:text-gray-400">
        Search and browse users to view their profile, costs, and quota status.
      </p>
    </div>

    <!-- Search Bar -->
    <div class="mb-6">
      <label for="user-search" class="sr-only">Search users by name or email</label>
      <div class="relative">
        <ng-icon
          name="heroMagnifyingGlass"
          class="absolute left-3 top-1/2 -translate-y-1/2 size-5 text-gray-400"
          aria-hidden="true"
        />
        <input
          id="user-search"
          type="search"
          autocomplete="off"
          spellcheck="false"
          [ngModel]="searchText()"
          (ngModelChange)="onSearchInput($event)"
          (keydown.enter)="searchNow()"
          placeholder="Search by name or email..."
          class="w-full pl-10 pr-16 py-2 bg-white border border-gray-300 rounded-sm focus:ring-2 focus:ring-primary-500 focus:border-primary-500 dark:bg-gray-800 dark:border-gray-500 dark:text-white dark:placeholder-gray-400 [&::-webkit-search-cancel-button]:hidden"
        />
        <div class="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-2">
          @if (state.loading() && (state.isSearching() || searchText())) {
            <app-spinner size="sm" label="Searching" />
          }
          @if (searchText()) {
            <button
              type="button"
              (click)="clearSearch()"
              aria-label="Clear search"
              class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded-xs focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-primary-accessible"
            >
              <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
            </button>
          }
        </div>
      </div>
    </div>

    <!-- Filters -->
    <div class="flex flex-wrap items-center gap-4 mb-6">
      <label for="user-status-filter" class="sr-only">Status</label>
      <select
        id="user-status-filter"
        [ngModel]="state.statusFilter()"
        (ngModelChange)="onStatusChange($event)"
        class="px-3 py-2 bg-white border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-500 dark:text-white"
      >
        <option value="all">All statuses</option>
        <option value="active">Active users</option>
        <option value="inactive">Inactive users</option>
        <option value="suspended">Suspended users</option>
      </select>

      @if (state.domains().length > 1) {
        <label for="user-domain-filter" class="sr-only">Email domain</label>
        <select
          id="user-domain-filter"
          [ngModel]="state.domainFilter() ?? ''"
          (ngModelChange)="onDomainChange($event)"
          class="px-3 py-2 bg-white border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-500 dark:text-white"
        >
          <option value="">All domains</option>
          @for (domain of state.domains(); track domain) {
            <option [value]="domain">{{ domain }}</option>
          }
        </select>
      }

      @if (state.totalCount() !== null && !state.hasError()) {
        <p class="text-sm/6 text-gray-600 dark:text-gray-400" aria-live="polite">
          @if (state.showingResults()) {
            {{ state.totalCount() }} {{ state.totalCount() === 1 ? 'match' : 'matches' }}
          } @else {
            {{ state.totalCount() }} {{ state.totalCount() === 1 ? 'user' : 'users' }}
          }
        </p>
      }
    </div>

    <!-- Error State -->
    @if (state.hasError()) {
      <div class="mb-6 p-4 bg-state-danger-50 border border-state-danger-200 rounded-sm text-state-danger-800 dark:bg-state-danger-900/20 dark:border-state-danger-800 dark:text-state-danger-200">
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
          <app-spinner size="xl" label="Loading users" />
          <p class="text-sm text-gray-500 dark:text-gray-400">
            Loading users...
          </p>
        </div>
      </div>
    }

    <!-- Duplicate profiles for one email -->
    @if (state.emailMatchCount() > 1) {
      <div
        role="status"
        class="mb-4 flex items-start gap-3 rounded-sm border border-state-warning-200 bg-state-warning-50 p-4 dark:border-state-warning-800 dark:bg-state-warning-900/20"
      >
        <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-state-warning-700 dark:text-state-warning-400" aria-hidden="true" />
        <p class="text-sm/6 text-state-warning-800 dark:text-state-warning-200">
          {{ state.emailMatchCount() }} profiles share this email. The first, with the most
          recent login, is the account that signs in; the others are leftovers from an earlier
          login setup. Quota overrides or tiers assigned to them reach nobody.
        </p>
      </div>
    }

    <!-- User List -->
    <div
      class="space-y-2 transition-opacity motion-reduce:transition-none"
      [class.opacity-60]="state.loading() && state.users().length > 0"
      [attr.aria-busy]="state.loading()"
    >
      @for (user of state.users(); track user.userId; let first = $first) {
        <div
          (click)="viewUser(user)"
          (keydown.enter)="viewUser(user)"
          tabindex="0"
          role="button"
          class="flex items-center gap-4 p-4 bg-white border border-gray-300 rounded-sm cursor-pointer hover:bg-gray-100 dark:bg-gray-800 dark:border-gray-600 dark:hover:bg-gray-700 transition-colors"
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
              @if (isDuplicateRow(user)) {
                <span
                  class="px-2 py-0.5 text-xs rounded-xs shrink-0"
                  [class]="first ? 'bg-state-success-100 text-state-success-700 dark:bg-state-success-900/30 dark:text-state-success-400' : 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'"
                >
                  {{ first ? 'Signs in' : 'Duplicate' }}
                </span>
              }
              <span
                class="px-2 py-0.5 text-xs rounded-xs shrink-0"
                [class]="getStatusClass(user.status)"
              >
                {{ statusLabel(user.status) }}
              </span>
            </div>
            <div class="text-sm/6 text-gray-500 dark:text-gray-400">
              {{ user.name || 'No name' }} &middot; Last login:
              {{ formatDate(user.lastLoginAt) }}
              @if (isDuplicateRow(user)) {
                &middot; ID: <span class="font-mono">{{ user.userId }}</span>
              }
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
    @if (state.users().length === 0 && !state.loading() && !state.hasError()) {
      <div class="text-center py-12 text-gray-500 dark:text-gray-400" role="status">
        <ng-icon name="heroUser" class="size-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" aria-hidden="true" />
        @if (state.showingResults()) {
          <p class="text-lg/7">No users match “{{ state.resultQuery() }}”</p>
          <p class="text-sm/6">
            Check the spelling or try part of a name or email.
            @if (state.statusFilter() !== 'all' || state.domainFilter()) {
              The status and domain filters also apply.
            }
          </p>
        } @else {
          <p class="text-lg/7">No users found</p>
          <p class="text-sm/6">Try adjusting your filters</p>
        }
      </div>
    }

    <!-- Load More -->
    @if (state.hasMore()) {
      <div class="mt-6 text-center">
        <button
          type="button"
          (click)="loadMore()"
          [disabled]="state.loading()"
          class="inline-flex items-center gap-2 px-4 py-2 text-primary-accessible hover:underline disabled:opacity-50 dark:text-primary-accessible-dark"
        >
          @if (state.loading()) {
            <app-spinner size="sm" label="Loading more users" />
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
  private destroyRef = inject(DestroyRef);

  /** What is typed in the box; the list follows it after a short pause, or at once on Enter. */
  searchText = signal(this.state.searchQuery());
  private searchInput$ = new Subject<string>();

  ngOnInit(): void {
    this.searchInput$
      .pipe(debounceTime(SEARCH_DEBOUNCE_MS), takeUntilDestroyed(this.destroyRef))
      .subscribe((text) => {
        if (text.trim() !== this.state.searchQuery()) {
          this.state.search(text);
        }
      });

    this.state.loadUsers(true);
    this.state.loadDomains();
  }

  onSearchInput(text: string): void {
    this.searchText.set(text ?? '');
    this.searchInput$.next(text ?? '');
  }

  /** Enter searches without waiting for the debounce. */
  searchNow(): void {
    this.state.search(this.searchText());
  }

  clearSearch(): void {
    this.searchText.set('');
    this.state.clearSearch();
  }

  onStatusChange(status: UserStatusFilter): void {
    this.state.setStatusFilter(status);
  }

  onDomainChange(domain: string): void {
    this.state.setDomainFilter(domain || null);
  }

  /** A row among several profiles sharing the searched email exactly. */
  isDuplicateRow(user: UserListItem): boolean {
    return this.state.emailMatchCount() > 1 && user.email.toLowerCase() === this.state.exactEmail();
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
    const date = parseIso(isoString);
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

  statusLabel(status: UserStatus): string {
    return STATUS_LABELS[status] ?? status;
  }

  getStatusClass(status: UserStatus): string {
    switch (status) {
      case 'suspended':
        return 'bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900 dark:text-state-danger-200';
      case 'inactive':
        return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200';
      default:
        return 'bg-state-success-100 text-state-success-800 dark:bg-state-success-900 dark:text-state-success-200';
    }
  }
}
