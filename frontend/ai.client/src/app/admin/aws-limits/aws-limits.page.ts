import { ChangeDetectionStrategy, Component, OnInit, computed, inject } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroArrowTopRightOnSquare } from '@ng-icons/heroicons/outline';
import { AwsLimit } from './models';
import { AwsLimitsStateService } from './services/aws-limits-state.service';
import { LimitsSummaryCardsComponent } from './components/limits-summary-cards.component';
import { LimitGroupTableComponent } from './components/limit-group-table.component';
import { IncreaseHistoryDialogComponent } from './components/increase-history-dialog.component';
import {
  RequestIncreaseDialogComponent,
  RequestIncreaseDialogData,
  RequestIncreaseDialogResult,
} from './components/request-increase-dialog.component';
import { ageLabel } from './utils/aws-limits-format';

const CHECKBOX =
  'size-4 cursor-pointer rounded accent-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:accent-primary-500';

/**
 * AWS Limits (GitHub issue #12): the account's Service Quotas for the services
 * this stack uses, with usage where AWS publishes it, and increase requests.
 * Not the per-user Quotas page: these are limits on the AWS account itself.
 */
@Component({
  selector: 'app-aws-limits-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, LimitsSummaryCardsComponent, LimitGroupTableComponent],
  providers: [provideIcons({ heroArrowPath, heroArrowTopRightOnSquare })],
  host: { class: 'block' },
  template: `
    <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">AWS Limits</h1>
        <p class="mt-1 text-gray-600 dark:text-gray-400">
          Service quotas on this AWS account for the services the stack uses.
          @if (dashboard(); as d) {
            <span>Region <strong class="font-medium text-gray-900 dark:text-white">{{ d.region }}</strong>.</span>
            <span>
              Usage is the high point of the last {{ d.usageWindowDays }} days: the busiest minute for requests and
              tokens, the highest count for things like concurrent sessions. Counts of what exists, such as
              Memories, are current. Hover a value to see where it came from.
            </span>
          }
        </p>
      </div>
      <div class="flex shrink-0 flex-col items-start gap-1 sm:items-end">
        <button
          type="button"
          (click)="refresh()"
          [disabled]="state.loading()"
          class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
          {{ state.loading() ? 'Refreshing…' : 'Refresh' }}
        </button>
        @if (dashboard(); as d) {
          <span class="text-sm/6 whitespace-nowrap text-gray-500 dark:text-gray-400" data-testid="as-of">
            Data as of {{ age(d.asOf) }}
          </span>
        }
      </div>
    </div>

    @if (state.loading()) {
      <p
        role="status"
        class="mb-6 rounded-2xl bg-state-info-50 px-4 py-3 text-sm/6 text-state-info-900 dark:bg-state-info-900/20 dark:text-state-info-200"
        data-testid="load-notice"
      >
        Reading quotas and usage from AWS. This reads about 2,000 quotas and a week of CloudWatch metrics,
        so the first load and each refresh can take 10 to 30 seconds. The results are then kept for 10 minutes.
      </p>
    }

    @if (dashboard()?.autoManagement; as auto) {
      <div
        class="mb-6 rounded-2xl px-4 py-3 text-sm/6"
        [class]="auto.optInStatus === 'ENABLED'
          ? 'bg-state-success-50 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200'
          : 'bg-state-info-50 text-state-info-900 dark:bg-state-info-900/20 dark:text-state-info-200'"
        data-testid="auto-management"
      >
        @if (auto.error) {
          The Service Quotas Automatic Management setting could not be read.
        } @else if (auto.optInStatus === 'ENABLED') {
          Service Quotas Automatic Management is on: AWS watches utilization and notifies you near a limit.
        } @else {
          Service Quotas Automatic Management is off. Turn it on to have AWS notify you before a quota runs out.
        }
        <a
          [href]="auto.consoleUrl"
          target="_blank"
          rel="noopener noreferrer"
          class="ml-1 inline-flex items-center gap-1 font-medium underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          Open in the console
          <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
          <span class="sr-only">(opens in a new tab)</span>
        </a>
      </div>
    }

    @if (state.error(); as message) {
      <p role="alert" class="mb-6 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
        {{ message }}
      </p>
    }

    @if (dashboard(); as d) {
      <app-limits-summary-cards
        class="mb-8"
        [summary]="d.summary"
        [windowDays]="d.usageWindowDays"
        (viewRequests)="openHistory()"
      />

      <div class="mb-6 flex flex-wrap items-center gap-x-6 gap-y-3" role="group" aria-label="Filter limits">
        <div class="flex items-center gap-2">
          <label for="aws-limits-group" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Group</label>
          <select
            id="aws-limits-group"
            [value]="state.filters().group"
            (change)="setGroup($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
          >
            <option value="">All groups</option>
            @for (g of d.groups; track g.id) {
              <option [value]="g.id">{{ g.label }}</option>
            }
          </select>
        </div>
        <div class="flex items-center gap-2">
          <input id="aws-limits-adjustable" type="checkbox" [class]="checkbox" [checked]="state.filters().adjustableOnly" (change)="setFilter('adjustableOnly', $event)" />
          <label for="aws-limits-adjustable" class="text-sm/6 text-gray-700 dark:text-gray-300">Adjustable only</label>
        </div>
        <div class="flex items-center gap-2">
          <input id="aws-limits-near" type="checkbox" [class]="checkbox" [checked]="state.filters().nearLimitOnly" (change)="setFilter('nearLimitOnly', $event)" />
          <label for="aws-limits-near" class="text-sm/6 text-gray-700 dark:text-gray-300">80% or more only</label>
        </div>
        <div class="flex items-center gap-2">
          <input id="aws-limits-all-bedrock" type="checkbox" [class]="checkbox" [checked]="state.showAllBedrock()" [disabled]="state.loading()" (change)="setAllBedrock($event)" />
          <label for="aws-limits-all-bedrock" class="text-sm/6 text-gray-700 dark:text-gray-300">Show all Bedrock quotas</label>
        </div>
        <div class="flex items-center gap-2">
          <input id="aws-limits-hide-unused" type="checkbox" [class]="checkbox" [checked]="state.filters().hideUnused" (change)="setFilter('hideUnused', $event)" />
          <label
            for="aws-limits-hide-unused"
            class="text-sm/6 text-gray-700 dark:text-gray-300"
            [title]="'Hides quotas with no use in the last ' + d.usageWindowDays + ' days, or that nothing measures'"
          >Hide unused</label>
        </div>
      </div>

      <div class="flex flex-col gap-10">
        @for (group of state.visibleGroups(); track group.id) {
          <app-limit-group-table
            [group]="group"
            [canRequest]="d.canRequestIncrease"
            [windowDays]="d.usageWindowDays"
            [guidanceUrl]="d.guidanceUrl"
            [emptyMessage]="emptyMessage(group.id)"
            (requestIncrease)="openRequest($event)"
          />
        } @empty {
          <p class="rounded-2xl border border-dashed border-gray-300 py-8 text-center text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">
            No limits match these filters.
          </p>
        }
      </div>

    }
  `,
})
export class AwsLimitsPage implements OnInit {
  protected readonly state = inject(AwsLimitsStateService);
  private dialog = inject(Dialog);

  protected readonly dashboard = computed(() => this.state.dashboard());
  protected readonly checkbox = CHECKBOX;
  protected readonly age = ageLabel;

  ngOnInit(): void {
    void this.state.load();
  }

  refresh(): Promise<void> {
    return this.state.refresh();
  }

  setGroup(event: Event): void {
    this.state.setFilters({ group: (event.target as HTMLSelectElement).value });
  }

  setFilter(key: 'adjustableOnly' | 'nearLimitOnly' | 'hideUnused', event: Event): void {
    this.state.setFilters({ [key]: (event.target as HTMLInputElement).checked });
  }

  setAllBedrock(event: Event): Promise<void> {
    return this.state.setShowAllBedrock((event.target as HTMLInputElement).checked);
  }

  emptyMessage(groupId: string): string {
    return groupId === 'model_inference' && !this.state.showAllBedrock()
      ? 'No Bedrock quotas match a model in the managed-model catalog. Turn on "Show all Bedrock quotas" to see them all.'
      : 'No quotas to show here.';
  }

  openHistory(): void {
    this.dialog.open(IncreaseHistoryDialogComponent);
  }

  async openRequest(limit: AwsLimit): Promise<void> {
    const d = this.dashboard();
    if (!d) return;
    const ref = this.dialog.open<RequestIncreaseDialogResult, RequestIncreaseDialogData>(RequestIncreaseDialogComponent, {
      data: { limit, region: d.region, guidanceUrl: d.guidanceUrl },
    });
    const result = await firstValueFrom(ref.closed);
    if (result) await this.state.recordSubmitted(result);
  }
}
