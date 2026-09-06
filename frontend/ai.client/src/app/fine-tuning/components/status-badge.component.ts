import { Component, ChangeDetectionStrategy, input, computed } from '@angular/core';

/**
 * Reusable status badge for fine-tuning job statuses.
 * Maps training/inference statuses to colored pill badges.
 */
@Component({
  selector: 'app-status-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span [class]="badgeClasses()">
      {{ status() }}
    </span>
  `,
})
export class StatusBadgeComponent {
  readonly status = input.required<string>();

  readonly badgeClasses = computed(() => {
    const base = 'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium';
    switch (this.status()) {
      case 'PENDING':
        return `${base} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
      case 'TRAINING':
      case 'TRANSFORMING':
        return `${base} bg-state-info-100 text-state-info-800 dark:bg-state-info-900/30 dark:text-state-info-300`;
      case 'COMPLETED':
        return `${base} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
      case 'FAILED':
        return `${base} bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/30 dark:text-state-danger-300`;
      case 'STOPPED':
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
      default:
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  });
}
