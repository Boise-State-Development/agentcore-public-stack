import { Component, ChangeDetectionStrategy, signal, computed, inject, OnInit } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaAssignment, QuotaAssignmentType } from '../../models/quota.models';

@Component({
  selector: 'app-assignment-list',
  imports: [RouterLink, FormsModule, DatePipe],
  templateUrl: './assignment-list.component.html',
  styleUrl: './assignment-list.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AssignmentListComponent implements OnInit {
  private quotaStateService = inject(QuotaStateService);

  // Filter signals
  searchQuery = signal<string>('');
  tierFilter = signal<string>('');
  typeFilter = signal<string>('');
  enabledFilter = signal<string>('');

  // Get data from service
  assignments = this.quotaStateService.assignments;
  tiers = this.quotaStateService.tiers;
  loading = this.quotaStateService.loadingAssignments;
  error = this.quotaStateService.error;

  // Filtered assignments
  readonly filteredAssignments = computed(() => {
    let assignments = this.assignments();
    const query = this.searchQuery().toLowerCase();
    const tier = this.tierFilter();
    const type = this.typeFilter();
    const enabled = this.enabledFilter();

    if (query) {
      assignments = assignments.filter(
        a =>
          a.assignmentId.toLowerCase().includes(query) ||
          (a.userId && a.userId.toLowerCase().includes(query)) ||
          (a.jwtRole && a.jwtRole.toLowerCase().includes(query)) ||
          (a.emailDomain && a.emailDomain.toLowerCase().includes(query))
      );
    }

    if (tier) {
      assignments = assignments.filter(a => a.tierId === tier);
    }

    if (type) {
      assignments = assignments.filter(a => a.assignmentType === type);
    }

    if (enabled) {
      const isEnabled = enabled === 'enabled';
      assignments = assignments.filter(a => a.enabled === isEnabled);
    }

    // Sort by priority (highest first)
    return assignments.sort((a, b) => b.priority - a.priority);
  });

  // Available assignment types for filter
  readonly assignmentTypes = [
    { value: QuotaAssignmentType.DIRECT_USER, label: 'Direct User' },
    { value: QuotaAssignmentType.JWT_ROLE, label: 'JWT Role' },
    { value: QuotaAssignmentType.EMAIL_DOMAIN, label: 'Email Domain' },
    { value: QuotaAssignmentType.DEFAULT_TIER, label: 'Default Tier' },
  ];

  // Check if any filters are active
  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.tierFilter() || this.typeFilter() || this.enabledFilter());
  });

  async ngOnInit() {
    await Promise.all([
      this.quotaStateService.loadTiers(),
      this.quotaStateService.loadAssignments()
    ]);
  }

  /**
   * Reset all filters
   */
  resetFilters(): void {
    this.searchQuery.set('');
    this.tierFilter.set('');
    this.typeFilter.set('');
    this.enabledFilter.set('');
  }

  /**
   * Delete an assignment
   */
  async deleteAssignment(assignmentId: string): Promise<void> {
    if (confirm('Are you sure you want to delete this assignment?')) {
      try {
        await this.quotaStateService.deleteAssignment(assignmentId);
      } catch (error) {
        console.error('Error deleting assignment:', error);
        alert('Failed to delete assignment. Please try again.');
      }
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
   * Get assignment type display label
   */
  getTypeLabel(type: QuotaAssignmentType): string {
    const typeObj = this.assignmentTypes.find(t => t.value === type);
    return typeObj ? typeObj.label : type;
  }

  /**
   * Get assignment value for display
   */
  getAssignmentValue(assignment: QuotaAssignment): string {
    switch (assignment.assignmentType) {
      case QuotaAssignmentType.DIRECT_USER:
        return assignment.userId || 'N/A';
      case QuotaAssignmentType.JWT_ROLE:
        return assignment.jwtRole || 'N/A';
      case QuotaAssignmentType.EMAIL_DOMAIN:
        return assignment.emailDomain || 'N/A';
      case QuotaAssignmentType.DEFAULT_TIER:
        return '(Default for all users)';
      default:
        return 'N/A';
    }
  }
}
