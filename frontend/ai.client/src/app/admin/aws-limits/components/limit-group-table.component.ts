import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowTopRightOnSquare } from '@ng-icons/heroicons/outline';
import { AwsLimit, AwsLimitGroup } from '../models';
import { formatLimitValue, raisedFromDefault, unitAndPeriod } from '../utils/aws-limits-format';
import { UtilizationBarComponent } from './utilization-bar.component';

/** One catalog group: its quotas, or its inline error, or its documentation links. */
@Component({
  selector: 'app-limit-group-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, UtilizationBarComponent],
  providers: [provideIcons({ heroArrowTopRightOnSquare })],
  host: { class: 'block' },
  template: `
    <section [attr.aria-labelledby]="'aws-limits-group-' + group().id">
      <div class="mb-3">
        <h2 [id]="'aws-limits-group-' + group().id" class="text-base/7 font-semibold text-gray-900 dark:text-white">
          {{ group().label }}
        </h2>
        <p class="text-sm/6 text-gray-600 dark:text-gray-400">{{ group().description }}</p>
      </div>

      @if (group().error; as message) {
        <p
          role="alert"
          class="rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200"
        >
          {{ message }}
        </p>
      }
      @if (group().usageError; as message) {
        <p class="mb-3 rounded-2xl bg-state-warning-50 px-4 py-3 text-sm/6 text-state-warning-900 dark:bg-state-warning-900/20 dark:text-state-warning-200">
          {{ message }}
        </p>
      }

      @if (group().docLinks.length) {
        <ul class="mb-3 flex flex-wrap gap-3">
          @for (link of group().docLinks; track link.url) {
            <li>
              <a
                [href]="link.url"
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex items-center gap-1 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
              >
                {{ link.label }}
                <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
                <span class="sr-only">(opens in a new tab)</span>
              </a>
            </li>
          }
        </ul>
      }

      @if (group().limits.length) {
        <div class="relative overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10">
          <table class="min-w-full divide-y divide-gray-200 text-left text-sm/6 dark:divide-white/10">
            <thead class="bg-gray-50 dark:bg-white/5">
              <tr>
                <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Quota</th>
                <th scope="col" class="px-4 py-2 text-right font-semibold text-gray-900 dark:text-white">Applied value</th>
                <th scope="col" class="px-4 py-2 text-right font-semibold text-gray-900 dark:text-white">{{ windowDays() }}-day peak</th>
                <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Peak utilization</th>
                <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Unit / period</th>
                <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Adjustable</th>
                <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white"><span class="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-200 bg-white dark:divide-white/10 dark:bg-gray-900">
              @for (limit of group().limits; track limit.serviceCode + limit.quotaCode) {
                <tr>
                  <td class="px-4 py-3">
                    <div class="font-medium text-gray-900 dark:text-white">{{ limit.quotaName }}</div>
                    <div class="text-xs/5 text-gray-500 dark:text-gray-400">{{ limit.serviceCode }} · {{ limit.quotaCode }}</div>
                  </td>
                  <td class="px-4 py-3 text-right tabular-nums text-gray-900 dark:text-white">
                    {{ value(limit.appliedValue) }}
                    @if (raised(limit)) {
                      <div class="text-xs/5 text-gray-500 dark:text-gray-400">AWS default {{ value(limit.defaultValue) }}</div>
                    }
                  </td>
                  <td
                    class="px-4 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300"
                    [attr.title]="limit.usageSource ?? 'Nothing in CloudWatch measures this quota'"
                  >
                    {{ limit.usage === null ? 'N/A' : value(limit.usage) }}
                  </td>
                  <td class="px-4 py-3"><app-utilization-bar [utilization]="limit.utilization" /></td>
                  <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ unit(limit) }}</td>
                  <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ limit.adjustable ? 'Yes' : 'No' }}</td>
                  <td class="px-4 py-3">
                    <div class="flex flex-wrap items-center justify-end gap-3 whitespace-nowrap">
                      @if (limit.requestable && canRequest()) {
                        <button
                          type="button"
                          (click)="requestIncrease.emit(limit)"
                          class="rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                        >
                          Request AWS increase<span class="sr-only"> for {{ limit.quotaName }}</span>
                        </button>
                      } @else if (!limit.adjustable) {
                        <a
                          [href]="guidanceUrl() + '#non-adjustable-quotas'"
                          target="_blank"
                          rel="noopener noreferrer"
                          class="text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
                        >
                          How to raise this<span class="sr-only"> ({{ limit.quotaName }}, opens in a new tab)</span>
                        </a>
                      }
                      <a
                        [href]="limit.consoleUrl"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="inline-flex items-center gap-1 text-sm/6 text-gray-600 hover:text-gray-900 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
                      >
                        Console
                        <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
                        <span class="sr-only">for {{ limit.quotaName }} (opens in a new tab)</span>
                      </a>
                    </div>
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      } @else if (!group().error && !group().docLinks.length) {
        <p class="rounded-2xl border border-dashed border-gray-300 py-6 text-center text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">
          {{ emptyMessage() }}
        </p>
      }
    </section>
  `,
})
export class LimitGroupTableComponent {
  readonly group = input.required<AwsLimitGroup>();
  /** Days the usage peak covers. */
  readonly windowDays = input(7);
  /** The caller may submit increases (server-decided). */
  readonly canRequest = input(false);
  readonly guidanceUrl = input('');
  readonly emptyMessage = input('No quotas to show here.');

  readonly requestIncrease = output<AwsLimit>();

  protected value = formatLimitValue;
  protected unit = unitAndPeriod;
  protected raised = raisedFromDefault;
}
