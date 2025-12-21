import { Component, ChangeDetectionStrategy, signal, computed, inject, OnInit } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaTier } from '../../models/quota.models';

@Component({
  selector: 'app-tier-list',
  imports: [RouterLink, FormsModule, DatePipe],
  templateUrl: './tier-list.component.html',
  styleUrl: './tier-list.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TierListComponent implements OnInit {
  private quotaStateService = inject(QuotaStateService);

  // Search and filter signals
  searchQuery = signal<string>('');
  enabledFilter = signal<string>('');

  // Get tiers from service
  tiers = this.quotaStateService.tiers;
  loading = this.quotaStateService.loadingTiers;
  error = this.quotaStateService.error;

  // Filtered tiers based on search and filters
  readonly filteredTiers = computed(() => {
    let tiers = this.tiers();
    const query = this.searchQuery().toLowerCase();
    const enabled = this.enabledFilter();

    if (query) {
      tiers = tiers.filter(
        t =>
          t.tierName.toLowerCase().includes(query) ||
          t.tierId.toLowerCase().includes(query) ||
          (t.description && t.description.toLowerCase().includes(query))
      );
    }

    if (enabled) {
      const isEnabled = enabled === 'enabled';
      tiers = tiers.filter(t => t.enabled === isEnabled);
    }

    return tiers;
  });

  // Check if any filters are active
  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.enabledFilter());
  });

  async ngOnInit() {
    await this.quotaStateService.loadTiers();
  }

  /**
   * Reset all filters
   */
  resetFilters(): void {
    this.searchQuery.set('');
    this.enabledFilter.set('');
  }

  /**
   * Delete a tier
   */
  async deleteTier(tierId: string, tierName: string): Promise<void> {
    if (confirm(`Are you sure you want to delete the tier "${tierName}"?`)) {
      try {
        await this.quotaStateService.deleteTier(tierId);
      } catch (error) {
        console.error('Error deleting tier:', error);
        alert('Failed to delete tier. Please try again.');
      }
    }
  }

  /**
   * Format number as currency
   */
  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(value);
  }

  /**
   * Get action on limit display text
   */
  getActionDisplay(action: 'block' | 'warn'): string {
    return action === 'block' ? 'Block' : 'Warn Only';
  }
}
