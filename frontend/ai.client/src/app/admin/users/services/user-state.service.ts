import { Injectable, inject, signal, computed } from '@angular/core';
import { UserHttpService } from './user-http.service';
import {
  UserListItem,
  UserDetailResponse,
  UserStatus,
} from '../models';

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
  searchQuery = signal('');
  statusFilter = signal<UserStatus>('active');
  domainFilter = signal<string | null>(null);
  nextCursor = signal<string | null>(null);
  error = signal<string | null>(null);
  /**
   * How many profiles the last email search matched. More than one means the
   * email owns legacy duplicate rows; the backend lists the live one first.
   * Zero whenever the list is not an email search result.
   */
  emailMatchCount = signal(0);

  // ========== Computed Signals ==========

  hasMore = computed(() => this.nextCursor() !== null);
  userCount = computed(() => this.users().length);
  hasError = computed(() => this.error() !== null);

  // ========== User List Methods ==========

  /**
   * Load users with current filters.
   * @param reset If true, clears existing users and starts fresh
   */
  async loadUsers(reset: boolean = false): Promise<void> {
    if (reset) {
      this.users.set([]);
      this.nextCursor.set(null);
      this.emailMatchCount.set(0);
    }

    this.loading.set(true);
    this.error.set(null);

    try {
      const response = await this.http
        .listUsers({
          status: this.statusFilter(),
          domain: this.domainFilter() ?? undefined,
          limit: 25,
          cursor: reset ? undefined : this.nextCursor() ?? undefined,
        })
        .toPromise();

      if (response) {
        if (reset) {
          this.users.set(response.users);
        } else {
          this.users.update((current) => [...current, ...response.users]);
        }
        this.nextCursor.set(response.nextCursor ?? null);
      }
    } catch (err: any) {
      this.error.set(err.message || 'Failed to load users');
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Search for a user by exact email match.
   */
  async searchByEmail(email: string): Promise<void> {
    this.loading.set(true);
    this.searchQuery.set(email);
    this.error.set(null);

    try {
      const response = await this.http.searchByEmail(email).toPromise();

      if (response) {
        this.users.set(response.users);
        this.nextCursor.set(null);
        this.emailMatchCount.set(response.users.length);
      }
    } catch (err: any) {
      this.error.set(err.message || 'Failed to search users');
    } finally {
      this.loading.set(false);
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
  setStatusFilter(status: UserStatus): void {
    this.statusFilter.set(status);
    this.loadUsers(true);
  }

  /**
   * Set domain filter and reload users.
   */
  setDomainFilter(domain: string | null): void {
    this.domainFilter.set(domain);
    this.loadUsers(true);
  }

  /**
   * Clear search and reload with filters.
   */
  clearSearch(): void {
    this.searchQuery.set('');
    this.loadUsers(true);
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
    this.users.set([]);
    this.selectedUser.set(null);
    this.loading.set(false);
    this.searchQuery.set('');
    this.statusFilter.set('active');
    this.domainFilter.set(null);
    this.nextCursor.set(null);
    this.error.set(null);
    this.emailMatchCount.set(0);
  }
}
