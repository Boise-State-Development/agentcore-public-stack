import { Component, ChangeDetectionStrategy, signal, computed, inject, OnInit } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe, TitleCasePipe } from '@angular/common';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaOverride } from '../../models/quota.models';

@Component({
  selector: 'app-override-list',
  imports: [RouterLink, FormsModule, DatePipe, TitleCasePipe],
  templateUrl: './override-list.component.html',
  styleUrl: './override-list.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OverrideListComponent implements OnInit {
  private quotaStateService = inject(QuotaStateService);

  // Filter signals
  searchQuery = signal<string>('');
  activeOnlyFilter = signal<boolean>(false);
  typeFilter = signal<string>('');

  // Get data from service
  overrides = this.quotaStateService.overrides;
  loading = this.quotaStateService.loadingOverrides;
  error = this.quotaStateService.error;

  // Computed current time for status determination
  private now = signal(new Date().toISOString());

  // Filtered overrides
  readonly filteredOverrides = computed(() => {
    let overrides = this.overrides();
    const query = this.searchQuery().toLowerCase();
    const activeOnly = this.activeOnlyFilter();
    const type = this.typeFilter();
    const currentTime = this.now();

    if (query) {
      overrides = overrides.filter(
        o =>
          o.userId.toLowerCase().includes(query) ||
          o.overrideId.toLowerCase().includes(query) ||
          o.reason.toLowerCase().includes(query)
      );
    }

    if (activeOnly) {
      overrides = overrides.filter(
        o => o.enabled && o.validFrom <= currentTime && o.validUntil >= currentTime
      );
    }

    if (type) {
      overrides = overrides.filter(o => o.overrideType === type);
    }

    // Sort by valid until date (soonest to expire first)
    return overrides.sort((a, b) => a.validUntil.localeCompare(b.validUntil));
  });

  // Check if any filters are active
  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.activeOnlyFilter() || this.typeFilter());
  });

  async ngOnInit() {
    await this.quotaStateService.loadOverrides();
    // Update current time every minute
    setInterval(() => this.now.set(new Date().toISOString()), 60000);
  }

  /**
   * Reset all filters
   */
  resetFilters(): void {
    this.searchQuery.set('');
    this.activeOnlyFilter.set(false);
    this.typeFilter.set('');
  }

  /**
   * Delete an override
   */
  async deleteOverride(overrideId: string, userId: string): Promise<void> {
    if (confirm(`Are you sure you want to delete the override for user "${userId}"?`)) {
      try {
        await this.quotaStateService.deleteOverride(overrideId);
      } catch (error) {
        console.error('Error deleting override:', error);
        alert('Failed to delete override. Please try again.');
      }
    }
  }

  /**
   * Get override status
   */
  getOverrideStatus(override: QuotaOverride): 'active' | 'expired' | 'upcoming' | 'disabled' {
    if (!override.enabled) {
      return 'disabled';
    }
    const now = this.now();
    if (override.validFrom > now) {
      return 'upcoming';
    }
    if (override.validUntil < now) {
      return 'expired';
    }
    return 'active';
  }

  /**
   * Get status badge color classes
   */
  getStatusClasses(status: string): string {
    switch (status) {
      case 'active':
        return 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-300';
      case 'expired':
        return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
      case 'upcoming':
        return 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300';
      case 'disabled':
        return 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-300';
      default:
        return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
    }
  }

  /**
   * Format currency
   */
  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(value);
  }
}
