import { Component, ChangeDetectionStrategy, signal, computed, inject, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe, DecimalPipe, JsonPipe } from '@angular/common';
import { QuotaHttpService } from '../../services/quota-http.service';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaEvent, QuotaEventType } from '../../models/quota.models';

@Component({
  selector: 'app-event-viewer',
  imports: [FormsModule, DatePipe, DecimalPipe, JsonPipe],
  templateUrl: './event-viewer.component.html',
  styleUrl: './event-viewer.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EventViewerComponent implements OnInit {
  private quotaHttpService = inject(QuotaHttpService);
  private quotaStateService = inject(QuotaStateService);

  // Make Object available in template
  readonly Object = Object;

  // Filter signals
  userIdFilter = signal<string>('');
  tierIdFilter = signal<string>('');
  eventTypeFilter = signal<string>('');
  limitFilter = signal<number>(50);

  // State
  events = signal<QuotaEvent[]>([]);
  loading = signal<boolean>(false);
  error = signal<string | null>(null);

  // Available tiers
  readonly tiers = this.quotaStateService.tiers;

  // Event types
  readonly eventTypes = [
    { value: QuotaEventType.WARNING, label: 'Warning' },
    { value: QuotaEventType.BLOCK, label: 'Block' },
    { value: QuotaEventType.RESET, label: 'Reset' },
    { value: QuotaEventType.OVERRIDE_APPLIED, label: 'Override Applied' },
  ];

  // Check if any filters are active
  readonly hasActiveFilters = computed(() => {
    return !!(this.userIdFilter() || this.tierIdFilter() || this.eventTypeFilter());
  });

  async ngOnInit() {
    await this.quotaStateService.loadTiers();
    await this.loadEvents();
  }

  /**
   * Load events with current filters
   */
  async loadEvents(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const options: any = {
        limit: this.limitFilter(),
      };

      if (this.userIdFilter()) options.userId = this.userIdFilter();
      if (this.tierIdFilter()) options.tierId = this.tierIdFilter();
      if (this.eventTypeFilter()) options.eventType = this.eventTypeFilter();

      const events = await this.quotaHttpService.getEvents(options).toPromise();
      this.events.set(events || []);
    } catch (err: any) {
      console.error('Error loading events:', err);
      this.error.set(err?.error?.detail || err?.message || 'Failed to load events');
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Reset all filters
   */
  resetFilters(): void {
    this.userIdFilter.set('');
    this.tierIdFilter.set('');
    this.eventTypeFilter.set('');
    this.limitFilter.set(50);
    this.loadEvents();
  }

  /**
   * Get event type badge classes
   */
  getEventTypeBadgeClasses(eventType: QuotaEventType): string {
    switch (eventType) {
      case QuotaEventType.WARNING:
        return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/50 dark:text-yellow-300';
      case QuotaEventType.BLOCK:
        return 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-300';
      case QuotaEventType.RESET:
        return 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300';
      case QuotaEventType.OVERRIDE_APPLIED:
        return 'bg-purple-100 text-purple-800 dark:bg-purple-900/50 dark:text-purple-300';
      default:
        return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
    }
  }

  /**
   * Get tier name by ID
   */
  getTierName(tierId: string): string {
    const tier = this.tiers().find(t => t.tierId === tierId);
    return tier ? tier.tierName : tierId;
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

  /**
   * Export events to CSV
   */
  exportToCSV(): void {
    const events = this.events();
    if (events.length === 0) {
      alert('No events to export');
      return;
    }

    // Create CSV content
    const headers = ['Event ID', 'User ID', 'Tier ID', 'Event Type', 'Current Usage', 'Quota Limit', 'Percentage Used', 'Timestamp'];
    const rows = events.map(e => [
      e.eventId,
      e.userId,
      e.tierId,
      e.eventType,
      e.currentUsage.toString(),
      e.quotaLimit.toString(),
      e.percentageUsed.toString(),
      e.timestamp,
    ]);

    const csvContent = [
      headers.join(','),
      ...rows.map(row => row.join(','))
    ].join('\n');

    // Download file
    const blob = new Blob([csvContent], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `quota-events-${new Date().toISOString().split('T')[0]}.csv`;
    link.click();
    window.URL.revokeObjectURL(url);
  }
}
