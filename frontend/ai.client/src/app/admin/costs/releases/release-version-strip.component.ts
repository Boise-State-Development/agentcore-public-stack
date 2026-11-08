import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { LedgerUnitKind } from '../models';
import { UnitChip } from './release-ledger.util';

/** Chips shown before "Show all": dev has a build per merge. */
const COLLAPSED = 12;

/**
 * One chip per version (or build) in the range, with its days and deploy
 * markers (release-ledger spec §6.4 item 1). Choosing a chip makes it the
 * candidate; the readout then picks the unit before it as the baseline.
 * Oldest first, so the strip reads in the order the releases shipped.
 */
@Component({
  selector: 'app-release-version-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section aria-labelledby="release-strip-heading">
      <div class="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id="release-strip-heading" class="text-sm/6 font-semibold text-gray-900 dark:text-white">
          {{ unit() === 'build' ? 'Builds' : 'Versions' }} in range
          <span class="font-normal text-gray-500 dark:text-gray-400">({{ chips().length }})</span>
        </h3>
        <p class="text-xs/5 text-gray-500 dark:text-gray-400">Choose a candidate. Its baseline is the one before it unless you pick another.</p>
      </div>

      @if (chips().length === 0) {
        <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">No ledger rows in this range.</p>
      } @else {
        <ul role="list" class="mt-3 flex flex-wrap gap-2">
          @if (hiddenCount() > 0) {
            <li>
              <button
                type="button"
                (click)="expanded.set(true)"
                class="inline-flex h-full items-center rounded-2xl border border-dashed border-gray-300 px-3 py-1.5 text-xs/5 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                Show {{ hiddenCount() }} older
              </button>
            </li>
          }
          @for (chip of visible(); track chip.key) {
            <li>
              <button
                type="button"
                [attr.aria-pressed]="chip.key === candidateKey()"
                [attr.aria-label]="chipLabel(chip)"
                [title]="chip.key"
                (click)="candidateChange.emit(chip.key)"
                class="flex flex-col items-start rounded-2xl border px-3 py-1.5 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="chipClass(chip)"
              >
                <span class="flex items-center gap-1.5 text-sm/5 font-medium">
                  <span class="font-mono">{{ chip.label }}</span>
                  @if (chip.key === candidateKey()) {
                    <span class="rounded-full bg-primary-accessible px-1.5 text-[0.6875rem]/4 font-semibold text-white">Candidate</span>
                  } @else if (chip.key === baselineKey()) {
                    <span class="rounded-full bg-gray-700 px-1.5 text-[0.6875rem]/4 font-semibold text-white dark:bg-gray-200 dark:text-gray-900">Baseline</span>
                  }
                </span>
                <span class="text-xs/5 text-gray-600 tabular-nums dark:text-gray-400">
                  {{ chip.sublabel }} · {{ chip.daysObserved }}d
                  @if (chip.deployMarkers.length > 0) {
                    · {{ chip.deployMarkers.length }} deploy{{ chip.deployMarkers.length === 1 ? '' : 's' }}
                  }
                  @if (chip.attributed) {
                    · mapped
                  }
                </span>
              </button>
            </li>
          }
        </ul>
      }
    </section>
  `,
})
export class ReleaseVersionStripComponent {
  chips = input.required<UnitChip[]>();
  unit = input.required<LedgerUnitKind>();
  candidateKey = input<string | null>(null);
  baselineKey = input<string | null>(null);
  candidateChange = output<string>();

  protected readonly expanded = signal(false);

  /** Always keeps the candidate and baseline visible, even when they are older than the newest twelve. */
  protected readonly visible = computed(() => {
    const chips = this.chips();
    if (this.expanded() || chips.length <= COLLAPSED) return chips;
    const keep = new Set([this.candidateKey(), this.baselineKey()]);
    const cutoff = chips.length - COLLAPSED;
    return chips.filter((c, i) => i >= cutoff || keep.has(c.key));
  });

  protected readonly hiddenCount = computed(() => this.chips().length - this.visible().length);

  protected chipClass(chip: UnitChip): string {
    if (chip.key === this.candidateKey()) {
      return 'border-primary-accessible bg-white text-gray-900 ring-1 ring-primary-accessible dark:bg-gray-900 dark:text-white';
    }
    if (chip.key === this.baselineKey()) {
      return 'border-gray-500 bg-white text-gray-900 dark:border-gray-400 dark:bg-gray-900 dark:text-white';
    }
    return 'border-gray-200 bg-white text-gray-900 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:hover:bg-gray-700';
  }

  protected chipLabel(chip: UnitChip): string {
    const role = chip.key === this.candidateKey() ? ', candidate' : chip.key === this.baselineKey() ? ', baseline' : '';
    const deploys = chip.deployMarkers.length
      ? `, deploy markers: ${chip.deployMarkers.map(m => m.service).join(', ')}`
      : '';
    const mapped = chip.attributed ? ', attributed by the deploy-time map' : '';
    const days = `${chip.daysObserved} day${chip.daysObserved === 1 ? '' : 's'}`;
    return `${chip.key}${role}: ${chip.firstDate} to ${chip.lastDate}, ${days}${deploys}${mapped}`;
  }
}
