import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { SystemSummaryCardComponent } from '../../costs/components/system-summary-card.component';
import { AwsLimitsSummary } from '../models';

@Component({
  selector: 'app-limits-summary-cards',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SystemSummaryCardComponent],
  host: { class: 'block' },
  template: `
    <div class="grid grid-cols-1 gap-6 sm:grid-cols-2 xl:grid-cols-4">
      <app-system-summary-card title="Limits monitored" [value]="'' + summary().monitored" icon="heroServerStack" />
      <app-system-summary-card
        title="At 80% or more"
        [value]="'' + summary().atOrAbove80"
        icon="heroExclamationTriangle"
        [detail]="windowDetail()"
      />
      <app-system-summary-card
        title="At 95% or more"
        [value]="'' + summary().atOrAbove95"
        icon="heroExclamationTriangle"
        [detail]="windowDetail()"
      />
      <app-system-summary-card
        title="Open increase requests"
        [value]="'' + summary().openRequests"
        icon="heroInboxArrowDown"
        detail="Pending or with a support case"
      >
        <button
          type="button"
          (click)="viewRequests.emit()"
          class="mt-2 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
        >
          View all<span class="sr-only"> increase requests</span>
        </button>
      </app-system-summary-card>
    </div>
  `,
})
export class LimitsSummaryCardsComponent {
  readonly summary = input.required<AwsLimitsSummary>();
  readonly windowDays = input(7);

  /** "View all" on the open requests card. */
  readonly viewRequests = output<void>();

  protected readonly windowDetail = computed(() => `Peak of the last ${this.windowDays()} days, where measured`);
}
