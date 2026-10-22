import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroHandThumbDown, heroArrowPath } from '@ng-icons/heroicons/outline';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { TooltipDirective } from '../../components/tooltip';
import { AdminFeedbackHttpService } from './services/admin-feedback-http.service';
import { FleetArm } from './fleet-feedback.models';
import {
  armComparison,
  barWidthPercent,
  coverageSummary,
  dimensionLabel,
  DIMENSION_HINTS,
  rateLabel,
  reasonLabel,
} from './fleet-feedback.util';

/**
 * Fleet feedback — down-thumb rate by config arm (response-feedback spec §7).
 *
 * The page is built around the one rule that keeps this useful rather than
 * dangerous (§9): **it never shows a fleet-wide quality number.** An absolute
 * rate over the few percent of turns that get thumbed is noise wearing a
 * KPI's clothes. What it shows is arms next to each other, each with its own
 * n, with any arm under the coverage floor rendered as "n < floor" instead of
 * a rate nobody should act on.
 */
@Component({
  selector: 'app-fleet-feedback',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, DatePipe, SpinnerComponent, TooltipDirective],
  providers: [provideIcons({ heroHandThumbDown, heroArrowPath })],
  template: `
    <div class="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <div class="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 class="text-2xl/8 font-semibold text-gray-900 dark:text-white">Feedback</h1>
          <p class="mt-1 max-w-3xl text-sm/6 text-gray-600 dark:text-gray-300">
            Where users pressed thumbs down, split by the configuration choices we made.
            Read the arms against each other, never the raw percentage on its own.
          </p>
        </div>
        <div class="flex items-center gap-2">
          <div class="flex items-center gap-1 rounded-2xl border border-gray-300 p-1 dark:border-gray-600" role="group" aria-label="Time window">
            @for (option of windowOptions; track option) {
              <button
                type="button"
                class="rounded-2xl px-3 py-1 text-sm/6 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="
                  days() === option
                    ? 'bg-primary-accessible text-white dark:bg-primary-accessible-dark dark:text-gray-900'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                "
                [attr.aria-pressed]="days() === option"
                (click)="days.set(option)"
              >
                {{ option }}d
              </button>
            }
          </div>
          <button
            type="button"
            class="inline-flex items-center justify-center rounded-2xl border border-gray-300 p-2 text-gray-600 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            appTooltip="Reload"
            appTooltipPosition="bottom"
            aria-label="Reload fleet feedback"
            (click)="fleetResource.reload()"
          >
            <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      @if (fleetResource.isLoading()) {
        <div class="flex justify-center py-16"><app-spinner /></div>
      } @else if (fleetResource.error()) {
        <div
          class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200"
        >
          Fleet feedback could not be loaded.
          <button type="button" (click)="fleetResource.reload()" class="ml-2 font-medium underline hover:no-underline">Retry</button>
        </div>
      } @else if (fleetResource.hasValue()) {
        @let fleet = fleetResource.value();

        @if (fleet.totals.thumbs === 0) {
          <div class="rounded-2xl border border-gray-200 bg-white px-4 py-12 text-center dark:border-gray-700 dark:bg-gray-800">
            <ng-icon name="heroHandThumbDown" class="mx-auto size-8 text-gray-400" aria-hidden="true" />
            <p class="mt-3 text-sm/6 font-medium text-gray-900 dark:text-white">No feedback in this window</p>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              Nobody has rated a response in the last {{ fleet.window.days }} days. Widen the window, or wait for the signal to accumulate.
            </p>
          </div>
        } @else {
          <!-- Totals. Counts, deliberately not a rate: see the class comment. -->
          <div class="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Thumbs</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ fleet.totals.thumbs }}</p>
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Up</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ fleet.totals.up }}</p>
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Down</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ fleet.totals.down }}</p>
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Conversations</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ fleet.coverage.sessionsWithFeedback }}</p>
            </div>
          </div>

          <p class="mb-6 rounded-2xl border border-state-info-200 bg-state-info-50 px-4 py-3 text-sm/6 text-state-info-800 dark:border-state-info-800 dark:bg-state-info-900/20 dark:text-state-info-200">
            Feedback is a sampler, not a score. Only a few percent of turns get rated and the sample skews negative,
            so these rates mean something only relative to each other. An arm needs {{ fleet.minimumN }} thumbs before
            a rate is shown at all.
          </p>

          <!-- One card per configuration dimension. -->
          <div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
            @for (dimension of dimensions(); track dimension.key) {
              <section class="rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                <div class="border-b border-gray-200 px-4 py-3 dark:border-gray-700">
                  <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">{{ label(dimension.key) }}</h2>
                  <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">{{ hint(dimension.key) }}</p>
                </div>

                @if (dimension.arms.length === 0) {
                  <p class="px-4 py-6 text-sm/6 text-gray-500 dark:text-gray-400">Nothing joined on this dimension yet.</p>
                } @else {
                  @if (comparison(dimension.arms); as gap) {
                    <p class="border-b border-gray-200 bg-gray-50 px-4 py-2 text-xs/5 text-gray-700 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300">
                      <span class="font-medium">{{ gap.gapPoints }} point gap</span>
                      between {{ gap.best.key }} ({{ rate(gap.best, fleet.minimumN) }})
                      and {{ gap.worst.key }} ({{ rate(gap.worst, fleet.minimumN) }})
                    </p>
                  }
                  <table class="w-full text-left">
                    <caption class="sr-only">{{ label(dimension.key) }} down-thumb rate by arm</caption>
                    <thead>
                      <tr class="text-xs/5 uppercase tracking-wide text-gray-500 dark:text-gray-400">
                        <th scope="col" class="px-4 py-2 font-medium">Arm</th>
                        <th scope="col" class="px-4 py-2 text-right font-medium">n</th>
                        <th scope="col" class="px-4 py-2 text-right font-medium">Down</th>
                        <th scope="col" class="px-4 py-2 font-medium">Down rate</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                      @for (arm of dimension.arms; track arm.key) {
                        <tr>
                          <td class="px-4 py-2 text-sm/6 text-gray-900 dark:text-white">
                            <span class="block max-w-[16rem] truncate" [title]="arm.key">{{ arm.key }}</span>
                          </td>
                          <td class="px-4 py-2 text-right text-sm/6 tabular-nums text-gray-600 dark:text-gray-300">{{ arm.n }}</td>
                          <td class="px-4 py-2 text-right text-sm/6 tabular-nums text-gray-600 dark:text-gray-300">{{ arm.down }}</td>
                          <td class="px-4 py-2">
                            @if (arm.belowFloor) {
                              <span class="text-sm/6 text-gray-400 dark:text-gray-500">{{ rate(arm, fleet.minimumN) }}</span>
                            } @else {
                              <div class="flex items-center gap-2">
                                <div class="h-2 w-24 overflow-hidden rounded-2xl bg-gray-200 dark:bg-gray-700" aria-hidden="true">
                                  <div class="h-full rounded-2xl bg-state-danger-500" [style.width.%]="barWidth(arm)"></div>
                                </div>
                                <span class="text-sm/6 font-medium tabular-nums text-gray-900 dark:text-white">{{ rate(arm, fleet.minimumN) }}</span>
                              </div>
                            }
                          </td>
                        </tr>
                      }
                    </tbody>
                  </table>
                }
              </section>
            }

            <!-- Why users said it was bad. Codes, never their words. -->
            <section class="rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
              <div class="border-b border-gray-200 px-4 py-3 dark:border-gray-700">
                <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Reasons given</h2>
                <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">
                  Fixed codes chosen on a thumbs down. No free text is ever collected.
                </p>
              </div>
              @if (reasonRows().length === 0) {
                <p class="px-4 py-6 text-sm/6 text-gray-500 dark:text-gray-400">No reasons recorded in this window.</p>
              } @else {
                <ul class="divide-y divide-gray-200 dark:divide-gray-700">
                  @for (row of reasonRows(); track row.key) {
                    <li class="flex items-center justify-between px-4 py-2">
                      <span class="text-sm/6 text-gray-900 dark:text-white">{{ reason(row.key) }}</span>
                      <span class="text-sm/6 tabular-nums text-gray-600 dark:text-gray-300">{{ row.count }}</span>
                    </li>
                  }
                </ul>
              }
            </section>
          </div>

          <p class="mt-6 text-xs/5 text-gray-500 dark:text-gray-400">
            {{ coverage(fleet.coverage) }} ·
            {{ fleet.window.start | date: 'mediumDate' }} to {{ fleet.window.end | date: 'mediumDate' }}
          </p>
          @if (fleet.coverage.truncated || fleet.coverage.sessionsOmitted > 0) {
            <p class="mt-1 text-xs/5 text-state-warning-700 dark:text-state-warning-300">
              This window hit a read cap, so the arms rest on a subset of it. Narrow the window for a complete picture.
            </p>
          }
        }
      }
    </div>
  `,
})
export class FleetFeedbackPage {
  private feedbackHttp = inject(AdminFeedbackHttpService);

  protected readonly windowOptions = [7, 30, 90] as const;
  readonly days = signal<number>(30);

  readonly fleetResource = resource({
    params: () => ({ days: this.days() }),
    loader: ({ params }) => firstValueFrom(this.feedbackHttp.getFleetFeedback(params.days)),
  });

  /** Dimensions in a fixed order, so the page does not reshuffle between loads. */
  readonly dimensions = computed(() => {
    const arms = this.fleetResource.hasValue() ? this.fleetResource.value().arms : {};
    const order = ['model', 'callsSinceCompaction', 'agentSwitch', 'turnClass'];
    const known = order.filter(key => key in arms);
    const extra = Object.keys(arms).filter(key => !order.includes(key)).sort();
    return [...known, ...extra].map(key => ({ key, arms: arms[key] ?? [] }));
  });

  readonly reasonRows = computed(() => {
    const reasons = this.fleetResource.hasValue() ? this.fleetResource.value().reasons : {};
    return Object.entries(reasons).map(([key, count]) => ({ key, count }));
  });

  protected label = dimensionLabel;
  protected reason = reasonLabel;
  protected hint = (key: string) => DIMENSION_HINTS[key] ?? '';
  protected rate = rateLabel;
  protected barWidth = barWidthPercent;
  protected comparison = armComparison;
  protected coverage = coverageSummary;

  /** Typed passthrough so the template can name the arm type. */
  protected trackArm(arm: FleetArm): string {
    return arm.key;
  }
}
