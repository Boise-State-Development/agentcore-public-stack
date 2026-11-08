import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroInformationCircle } from '@ng-icons/heroicons/outline';
import { LedgerReadout, ReadoutSide } from '../models';
import { UnitChip, caveatTone, formatPercent, unitLabel } from './release-ledger.util';
import { WINDOW_DAYS } from './release-ledger-state.service';

/**
 * Candidate vs baseline, their windows, the mix-shift warning and the
 * caveats, all before any number (release-ledger spec §6.4 item 2, D5).
 */
@Component({
  selector: 'app-release-compare-header',
  imports: [NgIcon],
  providers: [provideIcons({ heroExclamationTriangle, heroInformationCircle })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section
      aria-labelledby="release-compare-heading"
      class="rounded-2xl border border-gray-200 bg-white p-6 shadow-xs dark:border-gray-700 dark:bg-gray-800"
    >
      <h3 id="release-compare-heading" class="sr-only">Comparison</h3>
      <div class="grid grid-cols-1 gap-6 md:grid-cols-[1fr_1fr_auto]">
        <div>
          <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Baseline</p>
          @if (readout().baseline; as base) {
            <p class="mt-1 font-mono text-lg/7 font-semibold text-gray-900 dark:text-white" [title]="base.key">{{ label(base) }}</p>
            <p class="text-sm/6 text-gray-600 dark:text-gray-300">{{ windowText(base) }}</p>
          } @else {
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-300">None in range: nothing to compare.</p>
          }
          <label for="release-baseline" class="mt-3 block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Compare with</label>
          <select
            id="release-baseline"
            (change)="baselineChange.emit($any($event.target).value || null)"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
          >
            <option value="" [selected]="!readout().baseline">The one before the candidate</option>
            @for (chip of baselineOptions(); track chip.key) {
              <option [value]="chip.key" [selected]="chip.key === readout().baseline?.key">{{ chip.key }}</option>
            }
          </select>
        </div>

        <div>
          <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Candidate</p>
          <p class="mt-1 font-mono text-lg/7 font-semibold text-gray-900 dark:text-white" [title]="readout().candidate.key">
            {{ label(readout().candidate) }}
          </p>
          <p class="text-sm/6 text-gray-600 dark:text-gray-300">{{ windowText(readout().candidate) }}</p>
        </div>

        <div>
          <label for="release-window" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Window</label>
          <select
            id="release-window"
            (change)="windowDaysChange.emit(+$any($event.target).value)"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
          >
            @for (d of windowChoices; track d) {
              <option [value]="d" [selected]="d === readout().requestedDays">{{ d }} days</option>
            }
          </select>
          <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">Last full days of the baseline, first of the candidate.</p>
        </div>
      </div>

      @if (readout().mixShift.warn) {
        <div
          role="note"
          class="mt-6 rounded-2xl border border-state-warning-300 bg-state-warning-50 p-4 dark:border-state-warning-700 dark:bg-state-warning-900/30"
          data-testid="mix-shift"
        >
          <div class="flex gap-3">
            <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-5 shrink-0 text-state-warning-700 dark:text-state-warning-300" aria-hidden="true" />
            <div class="text-sm/6 text-state-warning-900 dark:text-state-warning-100">
              <p class="font-semibold">Mix shift: read this before any delta</p>
              <p>The work changed between the windows, so a cost delta may be the mix, not the code.</p>
              @if (mixModels().length > 0) {
                <ul role="list" class="mt-2 space-y-0.5">
                  @for (m of mixModels(); track m.modelId) {
                    <li class="tabular-nums"><span class="font-mono text-xs/5">{{ m.modelId }}</span>: {{ m.baseline }} → {{ m.candidate }} of calls</li>
                  }
                </ul>
              }
              @if (activeUsers(); as users) {
                <p class="mt-1 tabular-nums">Mean daily active users: {{ users }}</p>
              }
            </div>
          </div>
        </div>
      }

      @if (readout().caveats.length > 0) {
        <div class="mt-4">
          <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Caveats ({{ readout().caveats.length }})</h4>
          <ul role="list" class="mt-2 space-y-1.5" data-testid="caveats">
            @for (c of readout().caveats; track $index) {
              <li class="flex items-start gap-2 text-sm/6">
                @if (tone(c.code) === 'warning') {
                  <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-4 shrink-0 text-state-warning-700 dark:text-state-warning-300" aria-hidden="true" />
                } @else {
                  <ng-icon name="heroInformationCircle" class="mt-0.5 size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                }
                <span class="text-gray-800 dark:text-gray-200">
                  <code class="mr-1 rounded-md bg-gray-100 px-1 font-mono text-xs/5 text-gray-700 dark:bg-gray-700 dark:text-gray-200">{{ c.code }}</code>
                  {{ c.message }}
                </span>
              </li>
            }
          </ul>
        </div>
      }
    </section>
  `,
})
export class ReleaseCompareHeaderComponent {
  readout = input.required<LedgerReadout>();
  chips = input.required<UnitChip[]>();
  baselineChange = output<string | null>();
  windowDaysChange = output<number>();

  protected readonly windowChoices = WINDOW_DAYS;

  /** Only units that started before the candidate: a baseline is what came before. */
  protected readonly baselineOptions = computed(() => {
    const key = this.readout().candidate.key;
    const chips = this.chips();
    const index = chips.findIndex(c => c.key === key);
    return (index < 0 ? chips.filter(c => c.key !== key) : chips.slice(0, index)).slice().reverse();
  });

  protected readonly mixModels = computed(() =>
    Object.entries(this.readout().mixShift.models).map(([modelId, [b, c]]) => ({
      modelId,
      baseline: formatPercent(b),
      candidate: formatPercent(c),
    })),
  );

  protected readonly activeUsers = computed(() => {
    const [b, c] = this.readout().mixShift.activeUsers;
    if (b === null && c === null) return null;
    return `${b ?? '—'} → ${c ?? '—'}`;
  });

  protected label(side: ReadoutSide): string {
    return unitLabel(side.key);
  }

  protected windowText(side: ReadoutSide): string {
    const [from, to] = side.window;
    const span = from === to ? from : `${from} – ${to}`;
    const full = side.fullDays === side.days ? '' : `, ${side.fullDays} full`;
    return `${span} · ${side.days} day${side.days === 1 ? '' : 's'}${full}`;
  }

  protected tone(code: string): 'warning' | 'note' {
    return caveatTone(code);
  }
}
