import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowDownTray, heroArrowPath } from '@ng-icons/heroicons/outline';
import { CostTrendsChartComponent, TrendChartFormat } from '../components/cost-trends-chart.component';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { LedgerUnitKind } from '../models';
import { RANGE_DAYS, ReleaseLedgerStateService } from './release-ledger-state.service';
import { KpiFormat, TREND_METRICS, trendBands, trendPoints } from './release-ledger.util';
import { ReleaseVersionStripComponent } from './release-version-strip.component';
import { ReleaseCompareHeaderComponent } from './release-compare-header.component';
import { ReleaseKpiCardsComponent } from './release-kpi-cards.component';
import { ReleaseToolsTableComponent } from './release-tools-table.component';
import { ReleaseGroupsTableComponent } from './release-groups-table.component';
import { ReleaseQualityPanelComponent } from './release-quality-panel.component';

const CHART_FORMAT: Record<KpiFormat, TrendChartFormat> = {
  usd: 'usd-precise',
  percent: 'percent',
  ms: 'ms',
  chars: 'ms',
  number: 'ms',
};

/**
 * The Releases tab on `/admin/costs` (docs/specs/release-ledger.md §6.4).
 *
 * Renders the ledger's `versions`, `series`, `tools`, `quality` and `readout`
 * routes in the spec's order: the version strip, the compare header (mix
 * shift and caveats before any number), the KPI cards, the trend chart with
 * version bands, the tools, agents and projects tables, the quality panel and
 * the exports. Every number comes from the readout as returned; the tab
 * formats and orders, it never computes a comparison of its own (D10).
 *
 * Offered only while `features.releaseLedger` is on; the page's route already
 * requires `admin.costs`, which every ledger route checks again server-side.
 */
@Component({
  selector: 'app-releases-tab',
  imports: [
    NgIcon,
    SpinnerComponent,
    CostTrendsChartComponent,
    ReleaseVersionStripComponent,
    ReleaseCompareHeaderComponent,
    ReleaseKpiCardsComponent,
    ReleaseToolsTableComponent,
    ReleaseGroupsTableComponent,
    ReleaseQualityPanelComponent,
  ],
  providers: [provideIcons({ heroArrowDownTray, heroArrowPath })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-6">
      <!-- Controls: granularity and range. -->
      <div class="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div class="flex flex-wrap items-end gap-4">
          <fieldset>
            <legend class="text-xs/5 font-medium text-gray-700 dark:text-gray-300">Compare by</legend>
            <div class="mt-1 inline-flex rounded-2xl border border-gray-200 bg-gray-50 p-1 dark:border-gray-700 dark:bg-gray-800">
              @for (u of unitChoices; track u.id) {
                <button
                  type="button"
                  [attr.aria-pressed]="state.unit() === u.id"
                  (click)="setUnit(u.id)"
                  class="rounded-xl px-3 py-1 text-sm/6 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                  [class]="state.unit() === u.id
                    ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                    : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'"
                >
                  {{ u.label }}
                </button>
              }
            </div>
          </fieldset>
          <div>
            <label for="release-range" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Range</label>
            <select
              id="release-range"
              (change)="setRange(+$any($event.target).value)"
              class="mt-1 block rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
            >
              @for (d of rangeChoices; track d) {
                <option [value]="d" [selected]="d === state.rangeDays()">Last {{ d }} days</option>
              }
            </select>
          </div>
        </div>
        <div class="flex flex-wrap gap-2">
          <button
            type="button"
            (click)="state.exportReadout()"
            [disabled]="!state.readout()"
            class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroArrowDownTray" class="size-4" aria-hidden="true" />
            Readout JSON
          </button>
          <button
            type="button"
            (click)="state.exportNdjson()"
            [disabled]="state.loading()"
            class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroArrowDownTray" class="size-4" aria-hidden="true" />
            Ledger rows (NDJSON)
          </button>
        </div>
      </div>

      @if (state.error(); as error) {
        <div role="alert" class="rounded-2xl border border-state-danger-200 bg-state-danger-50 p-4 text-sm/6 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
          {{ error }}
          <button type="button" (click)="state.load()" class="ml-2 font-medium underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">Try again</button>
        </div>
      }

      @if (state.loading() && !state.versions()) {
        <div class="flex h-48 items-center justify-center">
          <app-spinner size="lg" label="Loading the release ledger" />
        </div>
      } @else {
        <app-release-version-strip
          [chips]="state.chips()"
          [unit]="state.unit()"
          [candidateKey]="state.candidateKey()"
          [baselineKey]="state.baselineKey()"
          (candidateChange)="state.selectCandidate($event)"
        />

        @if (state.readoutError(); as error) {
          <div role="alert" class="rounded-2xl border border-state-danger-200 bg-state-danger-50 p-4 text-sm/6 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
            {{ error }}
          </div>
        }

        <div class="relative" [attr.aria-busy]="state.loadingReadout()">
          @if (state.loadingReadout()) {
            <p class="mb-2 inline-flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
              <ng-icon name="heroArrowPath" class="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              Loading the readout…
            </p>
          }
          @if (state.readout(); as readout) {
            <div class="space-y-6" [class.opacity-60]="state.loadingReadout()">
              <app-release-compare-header
                [readout]="readout"
                [chips]="state.chips()"
                (baselineChange)="state.selectBaseline($event)"
                (windowDaysChange)="state.setWindowDays($event)"
              />
              <app-release-kpi-cards [readout]="readout" />
            </div>
          }
        </div>

        <section aria-labelledby="release-trend-controls" class="space-y-3">
          <div class="flex flex-wrap items-end justify-between gap-3">
            <h3 id="release-trend-controls" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Trend</h3>
            <div>
              <label for="release-trend-metric" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Plot</label>
              <select
                id="release-trend-metric"
                (change)="trendMetricKey.set($any($event.target).value)"
                class="mt-1 block rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
              >
                @for (m of trendMetrics; track m.key) {
                  <option [value]="m.key" [selected]="m.key === trendMetricKey()">{{ m.label }}</option>
                }
              </select>
            </div>
          </div>
          <app-cost-trends-chart
            [data]="trend()"
            [bands]="bands()"
            [heading]="trendMetric().label + ' by day'"
            [description]="trendDescription()"
            [primaryLabel]="trendMetric().label"
            [primaryFormat]="trendFormat()"
            secondaryLabel="Calls"
            [showSummary]="false"
            emptyText="No ledger rows in this range"
          />
        </section>

        @if (state.readout(); as readout) {
          <app-release-tools-table [tools]="readout.tools" [notMeasured]="state.tools()?.notMeasured ?? []" />
          <div class="grid grid-cols-1 gap-6 2xl:grid-cols-2">
            <app-release-groups-table heading="Agents" idLabel="Agent" [groups]="readout.agents" emptyText="No agent turns in either window." />
            <app-release-groups-table heading="Projects" idLabel="Project" [groups]="readout.projects" emptyText="No project turns in either window." />
          </div>
          <app-release-quality-panel [readout]="readout" [range]="state.quality()" />
        }
      }
    </div>
  `,
})
export class ReleasesTabComponent implements OnInit {
  protected readonly state = inject(ReleaseLedgerStateService);

  protected readonly unitChoices: ReadonlyArray<{ id: LedgerUnitKind; label: string }> = [
    { id: 'version', label: 'Version' },
    { id: 'build', label: 'Build' },
  ];
  protected readonly rangeChoices = RANGE_DAYS;
  protected readonly trendMetrics = TREND_METRICS;

  protected readonly trendMetricKey = signal(TREND_METRICS[0].key);
  protected readonly trendMetric = computed(
    () => TREND_METRICS.find(m => m.key === this.trendMetricKey()) ?? TREND_METRICS[0],
  );
  protected readonly trendFormat = computed(() => CHART_FORMAT[this.trendMetric().format]);

  protected readonly trend = computed(() => trendPoints(this.state.series()?.series ?? [], this.trendMetric()));
  protected readonly bands = computed(() => trendBands(this.state.series()?.bands ?? []));

  protected readonly trendDescription = computed(() => {
    const omitted = this.state.series()?.omittedKeys ?? 0;
    const base =
      this.trendMetric().kind === 'sum'
        ? 'Summed across every version on each day, with each version’s days shaded.'
        : 'A rate is shown only on days one version had to itself; a deploy day, or a day under the floor, is left blank.';
    return omitted ? `${base} ${omitted} smaller versions are left out of the series.` : base;
  });

  ngOnInit(): void {
    void this.state.ensureLoaded();
  }

  protected setUnit(unit: LedgerUnitKind): void {
    this.state.setUnit(unit);
  }

  protected setRange(days: number): void {
    this.state.setRangeDays(days);
  }
}
