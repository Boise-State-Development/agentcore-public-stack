import { Component, ChangeDetectionStrategy, signal, computed, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe, DecimalPipe } from '@angular/common';
import { QuotaHttpService } from '../../services/quota-http.service';
import { UserQuotaInfo } from '../../models/quota.models';
import { SpinnerComponent } from '../../../../components/spinner/spinner.component';

@Component({
  selector: 'app-quota-inspector',
  imports: [FormsModule, DatePipe, DecimalPipe, SpinnerComponent],
  templateUrl: './quota-inspector.component.html',
  styleUrl: './quota-inspector.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QuotaInspectorComponent {
  private quotaHttpService = inject(QuotaHttpService);

  // Search state
  userIdInput = signal<string>('');
  isLoading = signal<boolean>(false);
  error = signal<string | null>(null);
  quotaInfo = signal<UserQuotaInfo | null>(null);

  // Computed values
  readonly percentageUsed = computed(() => {
    const info = this.quotaInfo();
    return info ? info.percentageUsed : 0;
  });

  readonly remaining = computed(() => {
    const info = this.quotaInfo();
    return info?.remaining ?? 0;
  });

  readonly warningLevel = computed(() => {
    const percentage = this.percentageUsed();
    if (percentage >= 90) return 'critical';
    if (percentage >= 80) return 'warning';
    return 'normal';
  });

  /**
   * Search for user quota info
   */
  async searchUser(): Promise<void> {
    const userId = this.userIdInput().trim();
    if (!userId) {
      this.error.set('Please enter a user ID');
      return;
    }

    this.isLoading.set(true);
    this.error.set(null);
    this.quotaInfo.set(null);

    try {
      const info = await this.quotaHttpService.getUserQuotaInfo(userId).toPromise();
      if (info) {
        this.quotaInfo.set(info);
      }
    } catch (err: any) {
      console.error('Error fetching quota info:', err);
      this.error.set(err?.error?.detail || err?.message || 'Failed to fetch quota information');
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Clear the search
   */
  clearSearch(): void {
    this.userIdInput.set('');
    this.quotaInfo.set(null);
    this.error.set(null);
  }

  /**
   * Get progress bar color classes
   */
  getProgressBarClasses(): string {
    const level = this.warningLevel();
    switch (level) {
      case 'critical':
        return 'bg-state-danger-500';
      case 'warning':
        return 'bg-state-warning-500';
      default:
        return 'bg-state-success-500';
    }
  }

  /**
   * Get warning level badge classes
   */
  getWarningBadgeClasses(): string {
    const level = this.warningLevel();
    switch (level) {
      case 'critical':
        return 'bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/50 dark:text-state-danger-300';
      case 'warning':
        return 'bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/50 dark:text-state-warning-300';
      default:
        return 'bg-state-success-100 text-state-success-800 dark:bg-state-success-900/50 dark:text-state-success-300';
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

  /**
   * Get matched by display text
   */
  getMatchedByDisplay(matchedBy?: string): string {
    if (!matchedBy) return 'Unknown';
    const displayMap: Record<string, string> = {
      'override': 'Active Override',
      'direct_user': 'Direct User Assignment',
      'jwt_role': 'JWT Role Assignment',
      'email_domain': 'Email Domain Assignment',
      'default': 'Default Tier',
    };
    return displayMap[matchedBy] || matchedBy;
  }
}
