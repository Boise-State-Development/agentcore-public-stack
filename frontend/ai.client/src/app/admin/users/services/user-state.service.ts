import { Injectable, inject, signal, computed } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { UserHttpService } from './user-http.service';
import {
  UserListItem,
  UserDetailResponse,
  UserStatusFilter,
} from '../models';

const PAGE_SIZE = 25;

/**
 * State management service for user admin using signals.
 * Provides reactive state for user data, loading states, and errors.
 */
@Injectable({
  providedIn: 'root',
})
export class UserStateService {
  private http = inject(UserHttpService);

  // ========== State Signals ==========

  users = signal<UserListItem[]>([]);
  selectedUser = signal<UserDetailResponse | null>(null);
  loading = signal(false);
  /** The query asked for; empty for the plain list. */
  searchQuery = signal('');
  /** The query the shown rows answer. Trails `searchQuery` while a search is in flight. */
  resultQuery = signal('');
  statusFilter = signal<UserStatusFilter>('all');
  domainFilter = signal<string | null>(null);
  /** Email domains for the domain filter, the most users first. */
  domains = signal<string[]>([]);
  nextCursor = signal<string | null>(null);
  /** How many users match in all, across pages, when the backend reports it. */
  totalCount = signal<number | null>(null);
  error = signal<string | null>(null);

  /** Bumped per list request so a slow, superseded response never overwrites a newer one. */
  private requestSeq = 0;

  // ========== Computed Signals ==========

  hasMore = computed(() => this.nextCursor() !== null);
  userCount = computed(() => this.users().length);
  hasError = computed(() => this.error() !== null);
  isSearching = computed(() => this.searchQuery() !== '');
  /** The shown rows are search results (not the plain list). */
  showingResults = computed(() => this.resultQuery() !== '');

  /**
   * How many profiles share the searched email exactly. More than one means
   * the email owns legacy duplicate rows; the backend lists them first, the
   * live one on top. Zero unless the query is a whole email.
   */
  emailMatchCount = computed(() => {
    const email = this.exactEmail();
    return email ? this.users().filter((u) => u.email.toLowerCase() === email).length : 0;
  });

  /** The searched email when the query is one, lowercased; else null. */
  exactEmail = computed(() => {
    const query = this.resultQuery().toLowerCase();
    return query.includes('@') ? query : null;
  });

  // ========== User List Methods ==========

  /**
   * Load the list for the current query and filters: search results when
   * there is a query, every user otherwise.
   * @param reset If true, starts from the first page; otherwise appends the next one
   */
  async loadUsers(reset: boolean = false): Promise<void> {
    const seq = ++this.requestSeq;
    if (reset) {
      // The shown rows stay (dimmed) until the new ones arrive, so typing does not flash the list.
      this.nextCursor.set(null);
    }

    this.loading.set(true);
    this.error.set(null);

    const query = this.searchQuery();
    const options = {
      status: this.statusFilter(),
      domain: this.domainFilter() ?? undefined,
      limit: PAGE_SIZE,
      cursor: reset ? undefined : this.nextCursor() ?? undefined,
    };

    try {
      const response = await firstValueFrom(
        query ? this.http.searchUsers({ ...options, q: query }) : this.http.listUsers(options),
      );
      if (seq !== this.requestSeq) {
        return;
      }
      if (reset) {
        this.users.set(response.users);
        this.resultQuery.set(query);
      } else {
        this.users.update((current) => [...current, ...response.users]);
      }
      this.nextCursor.set(response.nextCursor ?? null);
      this.totalCount.set(response.totalCount ?? null);
    } catch {
      if (seq === this.requestSeq) {
        if (reset) {
          this.users.set([]);
          this.totalCount.set(null);
          this.resultQuery.set(query);
        }
        this.error.set(query ? 'Search failed. Please try again.' : 'Failed to load users. Please try again.');
      }
    } finally {
      if (seq === this.requestSeq) {
        this.loading.set(false);
      }
    }
  }

  /**
   * Search by part of a name, email or user id, keeping the current filters.
   * A blank query shows the plain list again.
   */
  search(query: string): Promise<void> {
    this.searchQuery.set(query.trim());
    return this.loadUsers(true);
  }

  /** Load the email domains for the domain filter. Best-effort: on failure the filter just has no options. */
  async loadDomains(): Promise<void> {
    try {
      this.domains.set(await firstValueFrom(this.http.listDomains()));
    } catch {
      this.domains.set([]);
    }
  }

  // ========== User Detail Methods ==========

  /**
   * Load detailed information for a specific user.
   */
  async loadUserDetail(userId: string): Promise<void> {
    this.loading.set(true);
    this.selectedUser.set(null);
    this.error.set(null);

    try {
      const detail = await this.http.getUserDetail(userId).toPromise();

      if (detail) {
        this.selectedUser.set(detail);
      }
    } catch (err: any) {
      this.error.set(err.message || 'Failed to load user detail');
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Clear selected user.
   */
  clearSelection(): void {
    this.selectedUser.set(null);
  }

  // ========== Filter Methods ==========

  /**
   * Set status filter and reload users.
   */
  setStatusFilter(status: UserStatusFilter): void {
    this.statusFilter.set(status);
    this.loadUsers(true);
  }

  /**
   * Set domain filter and reload users.
   */
  setDomainFilter(domain: string | null): void {
    this.domainFilter.set(domain || null);
    this.loadUsers(true);
  }

  /**
   * Clear search and reload with filters.
   */
  clearSearch(): void {
    this.search('');
  }

  // ========== Utility Methods ==========

  /**
   * Clear error state.
   */
  clearError(): void {
    this.error.set(null);
  }

  /**
   * Reset all state to initial values.
   */
  reset(): void {
    this.requestSeq++;
    this.users.set([]);
    this.selectedUser.set(null);
    this.loading.set(false);
    this.searchQuery.set('');
    this.resultQuery.set('');
    this.statusFilter.set('all');
    this.domainFilter.set(null);
    this.domains.set([]);
    this.nextCursor.set(null);
    this.totalCount.set(null);
    this.error.set(null);
  }
}
