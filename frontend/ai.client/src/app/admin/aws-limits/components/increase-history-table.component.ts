import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowTopRightOnSquare } from '@ng-icons/heroicons/outline';
import { IncreaseRequestRecord } from '../models';
import { formatDateTime, formatLimitValue, statusClass, statusLabel } from '../utils/aws-limits-format';

/** Increase requests AWS still lists, with who asked through this app. */
@Component({
  selector: 'app-increase-history-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArrowTopRightOnSquare })],
  host: { class: 'block' },
  template: `
    @if (requests().length) {
      <div class="relative overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10">
        <table class="min-w-full divide-y divide-gray-200 text-left text-sm/6 dark:divide-white/10">
          <thead class="bg-gray-50 dark:bg-white/5">
            <tr>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Service</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Quota</th>
              <th scope="col" class="px-4 py-2 text-right font-semibold text-gray-900 dark:text-white">Requested value</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Status</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Requested by</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Created</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Last updated</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Case ID</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 bg-white dark:divide-white/10 dark:bg-gray-900">
            @for (request of requests(); track request.requestId) {
              <tr>
                <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ request.serviceName || request.serviceCode }}</td>
                <td class="px-4 py-3">
                  <div class="font-medium text-gray-900 dark:text-white">{{ request.quotaName || request.quotaCode }}</div>
                  <div class="text-xs/5 text-gray-500 dark:text-gray-400">{{ request.quotaCode }}</div>
                </td>
                <td class="px-4 py-3 text-right tabular-nums text-gray-900 dark:text-white">{{ value(request.desiredValue) }}</td>
                <td class="px-4 py-3">
                  <span class="inline-flex rounded-full px-2 py-0.5 text-xs/5 font-medium" [class]="pill(request.status)">
                    {{ label(request.status) }}
                  </span>
                </td>
                <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ requester(request) }}</td>
                <td class="px-4 py-3 whitespace-nowrap text-gray-700 dark:text-gray-300">{{ date(request.created) }}</td>
                <td class="px-4 py-3 whitespace-nowrap text-gray-700 dark:text-gray-300">{{ date(request.lastUpdated) }}</td>
                <td class="px-4 py-3 text-gray-700 dark:text-gray-300">
                  @if (request.caseId) {
                    {{ request.caseId }}
                  } @else {
                    <a
                      [href]="request.consoleUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="inline-flex items-center gap-1 text-gray-600 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400"
                    >
                      Console
                      <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
                      <span class="sr-only">(opens in a new tab)</span>
                    </a>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    } @else {
      <p class="rounded-2xl border border-dashed border-gray-300 py-8 text-center text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">
        {{ emptyMessage() }}
        <a
          [href]="guidanceUrl()"
          target="_blank"
          rel="noopener noreferrer"
          class="font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark"
        >How AWS limit increases work<span class="sr-only"> (opens in a new tab)</span></a>
      </p>
    }
  `,
})
export class IncreaseHistoryTableComponent {
  readonly requests = input.required<IncreaseRequestRecord[]>();
  readonly guidanceUrl = input('');
  readonly emptyMessage = input('No increase requests in this Region.');

  protected value = formatLimitValue;
  protected date = formatDateTime;
  protected label = statusLabel;
  protected pill = statusClass;

  protected requester(request: IncreaseRequestRecord): string {
    if (request.requestType === 'AutomaticManagement') return 'Automatic Management';
    return request.requestedBy ?? 'Outside this app';
  }
}
