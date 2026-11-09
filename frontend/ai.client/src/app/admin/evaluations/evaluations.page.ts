import { ChangeDetectionStrategy, Component, OnInit, computed, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroXMark } from '@ng-icons/heroicons/outline';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { CostTrendsChartComponent, TrendChartPoint } from '../costs/components/cost-trends-chart.component';
import { LedgerEvalTrack } from '../costs/models';
import { shortBuild } from '../costs/releases/release-ledger.util';
import {
  CHART_CATEGORICAL_PALETTE,
  CHART_SERIES_COLORS,
} from '../../shared/constants/chart-colors.constants';
import { EvalFlipMatrixComponent } from './components/eval-flip-matrix.component';
import { EvalGateGridComponent } from './components/eval-gate-grid.component';
import { EvalLineChartComponent, EvalLineSeries } from './components/eval-line-chart.component';
import { EvalRunDetailComponent } from './components/eval-run-detail.component';
import { EVAL_RANGE_DAYS, EvaluationsStateService } from './evaluations-state.service';
import {
  FAMILY_FLOOR,
  TRACKS,
  TRACK_LABELS,
  caseCount,
  familyTrends,
  formatCost,
  formatDuration,
  judgeTrends,
  runBuild,
  runId,
  runWhen,
} from './evaluations.util';

/** slate-500: the baseline reference reads in both themes without competing with the run line. */
const BASELINE_COLOR = '#64748b';

interface FamilyChart {
  family: string;
  subheading: string;
  warn: boolean;
  yMax: number;
  series: EvalLineSeries[];
}

/**
 * `/admin/evaluations` (docs/specs/eval-harness.md §16): the eval harness's
 * published runs over time, read from the release ledger's `LEDGER#EVAL` rows.
 *
 * Seven sections in §16.3's order: the header with the model and track
 * selectors and each track's latest run; gate history; family trends; the case
 * flip matrix; judge trends; run cost and time; run detail. Every chart is one
 * model (D22) and one track, and no number on the page pools families.
 *
 * Read-only. Offered only while `features.releaseLedger` is on (the route's
 * `canMatch`) and to holders of `admin.costs`, which the ledger route checks
 * again server-side before the flag.
 */
@Component({
  selector: 'app-evaluations-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    SpinnerComponent,
    CostTrendsChartComponent,
    EvalGateGridComponent,
    EvalLineChartComponent,
    EvalFlipMatrixComponent,
    EvalRunDetailComponent,
  ],
  providers: [EvaluationsStateService, provideIcons({ heroArrowPath, heroXMark })],
  host: { class: 'block' },
  template: `
    <!-- 1. Header -->
    <div class="mb-6">
      <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Evaluations</h1>
      <p class="mt-1 max-w-3xl text-gray-600 dark:text-gray-400">
        The eval harness's published runs, one model and one track at a time. Deterministic families gate; judge
        families advise. Nothing here averages across families or models.
      </p>
    </div>

    <div class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
      <div class="flex flex-wrap items-end gap-4">
        <div>
          <label for="eval-model" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Model</label>
          <select
            id="eval-model"
            (change)="state.setModel($any($event.target).value)"
            [disabled]="models().length === 0"
            class="mt-1 block max-w-xs rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            @for (m of models(); track m.modelId) {
              <option [value]="m.modelId" [selected]="m.modelId === state.modelId()">{{ m.modelId }} ({{ m.runs }})</option>
            }
          </select>
        </div>
        <fieldset>
          <legend class="text-xs/5 font-medium text-gray-700 dark:text-gray-300">Track</legend>
          <div class="mt-1 inline-flex flex-wrap rounded-2xl border border-gray-200 bg-gray-50 p-1 dark:border-gray-700 dark:bg-gray-800">
            @for (t of trackChoices(); track t.id) {
              <button
                type="button"
                [attr.aria-pressed]="state.track() === t.id"
                (click)="state.setTrack(t.id)"
                class="rounded-xl px-3 py-1 text-sm/6 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="state.track() === t.id
                  ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                  : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'"
              >
                {{ t.label }} <span class="tabular-nums text-gray-500 dark:text-gray-400">{{ t.count }}</span>
              </button>
            }
          </div>
        </fieldset>
        <div>
          <label for="eval-range" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Range</label>
          <select
            id="eval-range"
            (change)="state.setRangeDays(+$any($event.target).value)"
            class="mt-1 block rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            @for (d of rangeChoices; track d) {
              <option [value]="d" [selected]="d === state.rangeDays()">Last {{ d }} days</option>
            }
          </select>
        </div>
        @if (state.build(); as build) {
          <div class="flex items-center gap-2 rounded-2xl border border-primary-200 bg-primary-50 py-1 pl-3 pr-1 text-sm/6 text-primary-900 dark:border-primary-800 dark:bg-primary-900/30 dark:text-primary-100" data-testid="build-filter">
            <span>Build <span class="font-mono">{{ shortBuild(build) }}</span> only</span>
            <button
              type="button"
              (click)="state.clearBuild()"
              class="rounded-xl p-1 hover:bg-primary-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500 dark:hover:bg-primary-800/50"
              aria-label="Show every build"
            >
              <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
            </button>
          </div>
        }
      </div>
      @if (state.loading() && state.response()) {
        <p class="inline-flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
          <ng-icon name="heroArrowPath" class="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Loading…
        </p>
      }
    </div>

    @if (state.error(); as error) {
      <div role="alert" class="mb-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 p-4 text-sm/6 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
        {{ error }}
        <button type="button" (click)="state.load()" class="ml-2 font-medium underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">Try again</button>
      </div>
    }

    @if (state.loading() && !state.response()) {
      <div class="flex h-48 items-center justify-center">
        <app-spinner size="lg" label="Loading evaluation runs" />
      </div>
    } @else if (state.response()) {
      <div class="space-y-10">
        <!-- Latest run per track -->
        @if (latest().length > 0) {
          <section aria-labelledby="eval-latest-heading">
            <h2 id="eval-latest-heading" class="sr-only">Latest run per track</h2>
            <ul role="list" class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
              @for (l of latest(); track l.track) {
                <li class="rounded-2xl border border-gray-200 bg-white p-4 shadow-xs dark:border-gray-700 dark:bg-gray-800" [attr.data-track]="l.track">
                  <p class="flex items-baseline justify-between gap-2">
                    <span class="text-sm/6 font-semibold text-gray-900 dark:text-white">Latest {{ l.label }}</span>
                    <span class="text-sm/6 font-semibold" [class]="l.failed ? 'text-state-danger-700 dark:text-state-danger-300' : 'text-state-success-700 dark:text-state-success-300'">
                      gate {{ l.failed ? 'failed' : 'passed' }}
                    </span>
                  </p>
                  <p class="mt-0.5 text-xs/5 text-gray-600 dark:text-gray-400">{{ l.when }}</p>
                  <dl class="mt-2 grid grid-cols-3 gap-x-3 gap-y-1 text-xs/5">
                    @for (f of l.facts; track f.label) {
                      <div class="min-w-0" [class.col-span-3]="f.wide">
                        <dt class="text-gray-500 dark:text-gray-400">{{ f.label }}</dt>
                        <dd class="font-mono tabular-nums break-words text-gray-900 dark:text-white">{{ f.value }}</dd>
                      </div>
                    }
                  </dl>
                </li>
              }
            </ul>
          </section>
        } @else {
          <p class="rounded-2xl border-2 border-dashed border-gray-200 p-6 text-center text-sm/6 text-gray-500 dark:border-gray-700 dark:text-gray-400">
            No evaluation run was published in the last {{ state.rangeDays() }} days. <code class="font-mono">eval_harness.py score --publish</code> writes one.
          </p>
        }

        <!-- 2. Gate history -->
        <section aria-labelledby="eval-gates-heading" class="space-y-3">
          <div>
            <h2 id="eval-gates-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Gate history</h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              One row per family, one cell per {{ trackLabel() }} run, oldest on the left. Hover a cell for that run's report row; choose one to open the run.
            </p>
          </div>
          <app-eval-gate-grid [columns]="state.columns()" [selectedRunId]="state.selectedRunId()" (runSelect)="selectRun($event)" />
        </section>

        <!-- 3. Family trends -->
        <section aria-labelledby="eval-trends-heading" class="space-y-3">
          <div>
            <h2 id="eval-trends-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Family trends</h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Cases passed out of n per run, with the baseline's count dashed and each build's days shaded.
            </p>
          </div>
          @if (familyCharts().length === 0) {
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">No runs to draw.</p>
          } @else {
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-3">
              @for (c of familyCharts(); track c.family) {
                <app-eval-line-chart
                  [heading]="c.family"
                  [subheading]="c.subheading"
                  [subheadingWarn]="c.warn"
                  [labels]="labels()"
                  [dates]="dates()"
                  [series]="c.series"
                  [bands]="state.bands()"
                  [yMax]="c.yMax"
                />
              }
            </div>
          }
        </section>

        <!-- 4. Case flip matrix -->
        <section aria-labelledby="eval-cases-heading" class="space-y-3">
          <div class="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 id="eval-cases-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Cases</h2>
              <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                Each case of one family per run. Hover a cell for its failure codes.
              </p>
            </div>
            @if (state.families().length > 0) {
              <div>
                <label for="eval-family" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Family</label>
                <select
                  id="eval-family"
                  (change)="state.familySelection.set($any($event.target).value)"
                  class="mt-1 block rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 font-mono text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                >
                  @for (f of state.families(); track f) {
                    <option [value]="f" [selected]="f === state.family()">{{ f }}</option>
                  }
                </select>
              </div>
            }
          </div>
          @if (state.family(); as family) {
            <app-eval-flip-matrix [columns]="state.columns()" [family]="family" (runSelect)="selectRun($event)" />
          }
        </section>

        <!-- 5. Judge trends -->
        <section aria-labelledby="eval-judges-heading" class="space-y-3">
          <div>
            <h2 id="eval-judges-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Judge trends <span class="font-normal text-gray-600 dark:text-gray-400">(advisory, never a gate)</span>
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Each evaluator's mean for {{ state.family() ?? 'the chosen family' }}, from 0 to 1. Hover a point for its n.
            </p>
          </div>
          @if (judgeChart(); as chart) {
            <app-eval-line-chart
              [heading]="chart.heading"
              [labels]="labels()"
              [dates]="dates()"
              [series]="chart.series"
              [bands]="state.bands()"
              format="score"
              [yMax]="1"
              [compact]="false"
            />
          } @else {
            <p class="rounded-2xl border-2 border-dashed border-gray-200 p-6 text-center text-sm/6 text-gray-500 dark:border-gray-700 dark:text-gray-400">
              No judge means for this family in these runs. A run publishes them only when it was judged.
            </p>
          }
        </section>

        <!-- 6. Run cost and time -->
        <section aria-labelledby="eval-cost-heading" class="space-y-3">
          <h2 id="eval-cost-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Run cost and time</h2>
          <app-cost-trends-chart
            [data]="costPoints()"
            [bands]="state.bands()"
            heading="Spend and model calls per run"
            description="The run's own model calls, from its cost rows: not the platform's spend."
            primaryLabel="Spend"
            primaryFormat="usd-precise"
            secondaryLabel="Model calls"
            [showSummary]="false"
            emptyText="No runs in this range"
          />
          @if (state.runs().length > 0) {
            <div class="overflow-x-auto rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800">
              <table class="min-w-full text-sm/6" data-testid="run-table">
                <caption class="sr-only">Runs, newest first</caption>
                <thead class="bg-gray-50 text-xs/5 dark:bg-gray-900/40">
                  <tr class="text-left font-medium text-gray-600 dark:text-gray-300">
                    <th scope="col" class="px-4 py-2">Run</th>
                    <th scope="col" class="px-3 py-2">Build</th>
                    <th scope="col" class="px-3 py-2 text-right">k</th>
                    <th scope="col" class="px-3 py-2 text-right">Cases</th>
                    <th scope="col" class="px-3 py-2 text-right">Spend</th>
                    <th scope="col" class="px-3 py-2 text-right">Calls</th>
                    <th scope="col" class="px-3 py-2 text-right">Duration</th>
                    <th scope="col" class="px-4 py-2 text-right">Gate</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-100 tabular-nums dark:divide-gray-700/60">
                  @for (r of runRows(); track r.id) {
                    <tr [class]="r.id === state.selectedRunId() ? 'bg-primary-50/60 dark:bg-primary-900/20' : ''">
                      <th scope="row" class="px-4 py-1.5 text-left font-normal">
                        <button
                          type="button"
                          (click)="selectRun(r.id)"
                          [attr.aria-pressed]="r.id === state.selectedRunId()"
                          class="font-medium text-primary-700 underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-300"
                        >{{ r.when }}</button>
                      </th>
                      <td class="px-3 py-1.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">{{ r.build }}</td>
                      <td class="px-3 py-1.5 text-right text-gray-900 dark:text-white">{{ r.k }}</td>
                      <td class="px-3 py-1.5 text-right text-gray-900 dark:text-white">{{ r.cases }}</td>
                      <td class="px-3 py-1.5 text-right text-gray-900 dark:text-white">{{ r.cost }}</td>
                      <td class="px-3 py-1.5 text-right text-gray-900 dark:text-white">{{ r.calls }}</td>
                      <td class="px-3 py-1.5 text-right text-gray-900 dark:text-white">{{ r.duration }}</td>
                      <td class="px-4 py-1.5 text-right font-medium" [class]="r.failed ? 'text-state-danger-700 dark:text-state-danger-300' : 'text-state-success-700 dark:text-state-success-300'">
                        {{ r.failed ? 'failed' : 'passed' }}
                      </td>
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          }
        </section>

        <!-- 7. Run detail -->
        <section aria-labelledby="eval-detail-heading" class="space-y-3">
          <h2 id="eval-detail-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Run detail</h2>
          @if (state.selectedRun(); as run) {
            <app-eval-run-detail [run]="run" />
          } @else {
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Choose a run.</p>
          }
        </section>
      </div>
    }
  `,
})
export class EvaluationsPage implements OnInit {
  protected readonly state = inject(EvaluationsStateService);
  private readonly route = inject(ActivatedRoute);

  protected readonly rangeChoices = EVAL_RANGE_DAYS;
  protected readonly shortBuild = shortBuild;

  /** Every model with runs in the range, plus a linked-to model with none, so the select shows it. */
  protected readonly models = computed(() => {
    const listed = this.state.response()?.models ?? [];
    const chosen = this.state.modelId();
    return chosen && !listed.some(m => m.modelId === chosen) ? [{ modelId: chosen, runs: 0 }, ...listed] : listed;
  });

  protected readonly trackChoices = computed(() => {
    const counts = this.state.trackCounts();
    return TRACKS.filter(t => t !== 'untracked' || counts.untracked > 0).map(id => ({
      id,
      label: TRACK_LABELS[id],
      count: counts[id],
    }));
  });

  protected readonly trackLabel = computed(() => {
    const track = this.state.track();
    return track ? TRACK_LABELS[track].toLowerCase() : '';
  });

  protected readonly latest = computed(() =>
    this.state.latest().map(({ track, run }) => ({
      track,
      label: TRACK_LABELS[track as LedgerEvalTrack].toLowerCase(),
      failed: !!run.gateFailed,
      when: runWhen(run),
      facts: [
        { label: 'Build', value: runBuild(run), wide: true },
        { label: 'k', value: run.k !== undefined ? String(run.k) : '—', wide: false },
        { label: 'Cases', value: String(caseCount(run)), wide: false },
        { label: 'Spend', value: formatCost(run.costUsd), wide: false },
        { label: 'Duration', value: formatDuration(run.durationS), wide: false },
        { label: 'Calls', value: run.calls !== undefined ? String(run.calls) : '—', wide: false },
      ],
    })),
  );

  /** A label per run: its date, with the time added where two runs share a date. */
  protected readonly labels = computed(() => {
    const cols = this.state.columns();
    const perDate = new Map<string, number>();
    for (const c of cols) perDate.set(c.date, (perDate.get(c.date) ?? 0) + 1);
    return cols.map(c => ((perDate.get(c.date) ?? 0) > 1 ? `${c.short} ${c.when.slice(11, 16)}` : c.short));
  });

  protected readonly dates = computed(() => this.state.columns().map(c => c.date));

  protected readonly familyCharts = computed<FamilyChart[]>(() =>
    familyTrends(this.state.columns()).map(t => ({
      family: t.family,
      subheading: t.underFloor ? `n=${t.n}, under the ${FAMILY_FLOOR}-case floor` : `n=${t.n}`,
      warn: t.underFloor,
      yMax: t.maxN,
      series: [
        {
          label: 'Passed',
          color: CHART_SERIES_COLORS.cost,
          values: t.run,
          notes: t.ns.map(n => (n === null ? null : `of ${n}`)),
        },
        { label: 'Baseline', color: BASELINE_COLOR, values: t.base, reference: true },
      ],
    })),
  );

  protected readonly judgeChart = computed(() => {
    const family = this.state.family();
    if (!family) return null;
    const trends = judgeTrends(this.state.columns(), family);
    if (trends.length === 0) return null;
    return {
      heading: family,
      series: trends.map((t, i) => ({
        label: t.evaluator,
        color: CHART_CATEGORICAL_PALETTE[i % CHART_CATEGORICAL_PALETTE.length],
        values: t.run,
        notes: t.ns.map((n, j) => (n === null ? null : `n ${n}, baseline ${t.base[j]?.toFixed(2)}`)),
      })),
    };
  });

  protected readonly costPoints = computed<TrendChartPoint[]>(() =>
    this.state.columns().map(c => ({ date: c.date, totalCost: c.run.costUsd ?? null, totalRequests: c.run.calls ?? 0 })),
  );

  protected readonly runRows = computed(() =>
    this.state.runs().map((run, i) => ({
      id: runId(run, i),
      when: runWhen(run),
      build: runBuild(run),
      k: run.k ?? '—',
      cases: caseCount(run),
      cost: formatCost(run.costUsd),
      calls: run.calls ?? '—',
      duration: formatDuration(run.durationS),
      failed: !!run.gateFailed,
    })),
  );

  ngOnInit(): void {
    const q = this.route.snapshot.queryParamMap;
    this.state.select({
      model: q.get('model'),
      track: q.get('track'),
      build: q.get('build'),
      run: q.get('run'),
      family: q.get('family'),
    });
    void this.state.load();
  }

  protected selectRun(id: string): void {
    this.state.runSelection.set(id);
  }
}
