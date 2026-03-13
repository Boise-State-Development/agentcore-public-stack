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
        return `${base} bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300`;
      case 'TRAINING':
      case 'TRANSFORMING':
        return `${base} bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300`;
      case 'COMPLETED':
        return `${base} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`;
      case 'FAILED':
        return `${base} bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300`;
      case 'STOPPED':
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
      default:
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  });
}
