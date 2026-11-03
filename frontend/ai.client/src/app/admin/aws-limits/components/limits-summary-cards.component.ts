import { ChangeDetectionStrategy, Component, input } from '@angular/core';
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
        detail="Where AWS publishes usage"
      />
      <app-system-summary-card
        title="At 95% or more"
        [value]="'' + summary().atOrAbove95"
        icon="heroExclamationTriangle"
        detail="Where AWS publishes usage"
      />
      <app-system-summary-card
        title="Open increase requests"
        [value]="'' + summary().openRequests"
        icon="heroInboxArrowDown"
        detail="Pending or with a support case"
      />
    </div>
  `,
})
export class LimitsSummaryCardsComponent {
  readonly summary = input.required<AwsLimitsSummary>();
}
