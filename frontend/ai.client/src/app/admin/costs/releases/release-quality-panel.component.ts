import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import {
  LedgerEvalRun,
  LedgerPair,
  LedgerQualityResponse,
  LedgerSmokeRun,
  LedgerReadout,
} from '../models';
import { formatKpiValue, formatN, unitLabel } from './release-ledger.util';

interface EvalBlock {
  side: 'Baseline' | 'Candidate';
  unit: string;
  runs: EvalRunView[];
}

interface EvalRunView {
  id: string;
  title: string;
  gateFailed: boolean;
  /** Query params for `/admin/evaluations`, filtered to this run's build (eval-harness spec §16 D21). */
  link: Record<string, string>;
  families: { name: string; n: number; base: number; run: number; gained: number; lost: number; p: string; gate: string }[];
}

interface SmokeView {
  side: 'Baseline' | 'Candidate';
  unit: string;
  runs: { id: string; title: string; counts: string; firstToken: string; cache: string; failed: boolean }[];
}

interface ShareRow {
  label: string;
  pair: string;
  n: string;
}

interface RangeRun {
  key: string;
  label: string;
  evalRuns: number;
  evalFailed: number;
  smokeRuns: number;
  smokeFailed: number;
}

/** Most recent runs shown per side; the export has every one. */
const RUNS_PER_SIDE = 3;

const OUTCOME_LABELS: [string, string][] = [
  ['completed', 'Completed'],
  ['paused', 'Paused for the user'],
  ['stopped', 'Stopped'],
  ['error', 'Error'],
  ['interrupted', 'Interrupted (disconnect)'],
  ['emptyAnswer', 'Empty answer'],
];

/**
 * The quality panel (release-ledger spec §6.4 item 7, D6): each signal on its
 * own, with its own floor, and no composite score anywhere. Order follows §10
 * Q2: the eval gate and the smoke matrix first, the thumbs arm last, as counts
 * only while it is under its floor.
 */
@Component({
  selector: 'app-release-quality-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <section
      aria-labelledby="release-quality-heading"
      class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
    >
      <div class="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
        <h3 id="release-quality-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Quality</h3>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Each signal on its own, with its own floor, never combined: which one moved is the diagnosis.
        </p>
      </div>

      <div class="space-y-8 px-6 py-5">
        <!-- Eval gate: the §8 table, per run -->
        <div>
          <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Eval gate</h4>
          <div class="mt-2 grid grid-cols-1 gap-6 lg:grid-cols-2">
            @for (block of evalBlocks(); track block.side) {
              <div>
                <p class="text-xs/5 font-medium text-gray-600 dark:text-gray-300">{{ block.side }} · <span class="font-mono">{{ block.unit }}</span></p>
                @if (block.runs.length === 0) {
                  <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">No eval run published for this unit.</p>
                }
                @for (run of block.runs; track run.id) {
                  <div class="mt-2 overflow-x-auto rounded-xl border border-gray-200 dark:border-gray-700">
                    <table class="min-w-full text-xs/5">
                      <caption class="px-3 py-1.5 text-left text-gray-700 dark:text-gray-300">
                        {{ run.title }} ·
                        <span [class]="run.gateFailed ? 'font-semibold text-state-danger-700 dark:text-state-danger-300' : 'font-semibold text-state-success-700 dark:text-state-success-300'">
                          gate {{ run.gateFailed ? 'failed' : 'passed' }}
                        </span>
                        ·
                        <a
                          routerLink="/admin/evaluations"
                          [queryParams]="run.link"
                          class="font-medium text-primary-700 underline underline-offset-2 hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-300"
                          data-testid="eval-link"
                        >Trends for this build</a>
                      </caption>
                      <thead class="bg-gray-50 dark:bg-gray-900/40">
                        <tr class="text-left font-medium text-gray-600 dark:text-gray-300">
                          <th scope="col" class="px-3 py-1.5">Family</th>
                          <th scope="col" class="px-2 py-1.5 text-right">n</th>
                          <th scope="col" class="px-2 py-1.5 text-right">Base</th>
                          <th scope="col" class="px-2 py-1.5 text-right">Run</th>
                          <th scope="col" class="px-2 py-1.5 text-right">Gained</th>
                          <th scope="col" class="px-2 py-1.5 text-right">Lost</th>
                          <th scope="col" class="px-2 py-1.5 text-right">p</th>
                          <th scope="col" class="px-3 py-1.5 text-right">Gate</th>
                        </tr>
                      </thead>
                      <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
                        @for (f of run.families; track f.name) {
                          <tr class="tabular-nums text-gray-900 dark:text-white">
                            <th scope="row" class="px-3 py-1.5 text-left font-mono font-normal">{{ f.name }}</th>
                            <td class="px-2 py-1.5 text-right">{{ f.n }}</td>
                            <td class="px-2 py-1.5 text-right">{{ f.base }}</td>
                            <td class="px-2 py-1.5 text-right">{{ f.run }}</td>
                            <td class="px-2 py-1.5 text-right">{{ f.gained }}</td>
                            <td class="px-2 py-1.5 text-right">{{ f.lost }}</td>
                            <td class="px-2 py-1.5 text-right">{{ f.p }}</td>
                            <td class="px-3 py-1.5 text-right" [class]="f.gate === 'fail' ? 'font-semibold text-state-danger-700 dark:text-state-danger-300' : ''">{{ f.gate }}</td>
                          </tr>
                        }
                      </tbody>
                    </table>
                  </div>
                }
              </div>
            }
          </div>
        </div>

        <!-- Smoke matrix -->
        <div>
          <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Smoke matrix</h4>
          <div class="mt-2 grid grid-cols-1 gap-6 lg:grid-cols-2">
            @for (block of smokeBlocks(); track block.side) {
              <div>
                <p class="text-xs/5 font-medium text-gray-600 dark:text-gray-300">{{ block.side }} · <span class="font-mono">{{ block.unit }}</span></p>
                @if (block.runs.length === 0) {
                  <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">No smoke run published for this unit.</p>
                } @else {
                  <ul role="list" class="mt-1 space-y-1">
                    @for (run of block.runs; track run.id) {
                      <li class="text-sm/6 tabular-nums text-gray-800 dark:text-gray-200">
                        <span class="text-xs/5 text-gray-500 dark:text-gray-400">{{ run.title }}</span> ·
                        <span [class]="run.failed ? 'font-semibold text-state-danger-700 dark:text-state-danger-300' : ''">{{ run.counts }}</span>
                        @if (run.firstToken) { · {{ run.firstToken }} }
                        @if (run.cache) { · {{ run.cache }} }
                      </li>
                    }
                  </ul>
                }
              </div>
            }
          </div>
        </div>

        <!-- Shares, from the readout's windows -->
        <div>
          <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Turn signals <span class="font-normal text-gray-500 dark:text-gray-400">(baseline → candidate)</span></h4>
          <div class="mt-2 overflow-x-auto">
            <table class="min-w-full text-sm/6">
              <caption class="sr-only">Quality shares, baseline to candidate</caption>
              <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
                @for (row of shares(); track row.label) {
                  <tr>
                    <th scope="row" class="py-1.5 pr-3 text-left font-normal text-gray-700 dark:text-gray-300">{{ row.label }}</th>
                    <td class="px-3 py-1.5 text-right tabular-nums text-gray-900 dark:text-white">{{ row.pair }}</td>
                    <td class="py-1.5 pl-3 text-right text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">{{ row.n }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
          <dl class="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs/5 sm:grid-cols-3">
            @for (o of outcomes(); track o.label) {
              <div class="flex justify-between gap-2">
                <dt class="text-gray-600 dark:text-gray-400">{{ o.label }}</dt>
                <dd class="tabular-nums text-gray-900 dark:text-white">{{ o.pair }}</dd>
              </div>
            }
          </dl>
        </div>

        <!-- Thumbs last (§10 Q2) -->
        <div>
          <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Thumbs</h4>
          <p class="mt-1 text-sm/6 tabular-nums text-gray-800 dark:text-gray-200" data-testid="thumbs">{{ thumbs() }}</p>
        </div>

        @if (rangeRuns().length > 0) {
          <div>
            <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Published runs in range</h4>
            <ul role="list" class="mt-2 flex flex-wrap gap-2">
              @for (r of rangeRuns(); track r.key) {
                <li class="rounded-xl border border-gray-200 px-2.5 py-1 text-xs/5 tabular-nums text-gray-700 dark:border-gray-700 dark:text-gray-300">
                  <span class="font-mono">{{ r.label }}</span>:
                  {{ r.evalRuns }} eval{{ r.evalFailed ? ' (' + r.evalFailed + ' failed)' : '' }},
                  {{ r.smokeRuns }} smoke{{ r.smokeFailed ? ' (' + r.smokeFailed + ' with failures)' : '' }}
                </li>
              }
            </ul>
          </div>
        }
      </div>
    </section>
  `,
})
export class ReleaseQualityPanelComponent {
  readout = input.required<LedgerReadout>();
  /** The quality route over the range: which units have published runs at all. */
  range = input<LedgerQualityResponse | null>(null);

  private readonly sides = computed(() => {
    const r = this.readout();
    return {
      baseline: r.baseline ? unitLabel(r.baseline.key) : 'none',
      candidate: unitLabel(r.candidate.key),
    };
  });

  protected readonly evalBlocks = computed<EvalBlock[]>(() => {
    const q = this.readout().quality.eval;
    const s = this.sides();
    return [
      { side: 'Baseline', unit: s.baseline, runs: latest(q.baseline).map(evalView) },
      { side: 'Candidate', unit: s.candidate, runs: latest(q.candidate).map(evalView) },
    ];
  });

  protected readonly smokeBlocks = computed<SmokeView[]>(() => {
    const q = this.readout().quality.smoke;
    const s = this.sides();
    return [
      { side: 'Baseline', unit: s.baseline, runs: latest(q.baseline).map(smokeView) },
      { side: 'Candidate', unit: s.candidate, runs: latest(q.candidate).map(smokeView) },
    ];
  });

  protected readonly shares = computed<ShareRow[]>(() => {
    const q = this.readout().quality;
    const outcomeN = q.outcomes['outcomeTurns'];
    const outcomeNText = outcomeN ? `${formatN(outcomeN)} turns with an outcome` : '';
    return [
      { label: 'Empty-answer share', pair: pct(q.emptyAnswerShare), n: outcomeNText },
      { label: 'Interrupted share (stopped, error, disconnect)', pair: pct(q.interruptedShare), n: outcomeNText },
      { label: 'Paused share (not interrupted)', pair: pct(q.pausedShare), n: outcomeNText },
      { label: 'Tool error rate', pair: pct(q.toolErrorRate), n: '' },
      { label: 'Forced-compaction rate', pair: pct(q.forcedCompactionRate), n: '' },
      {
        label: 'Provenance: quote_unsupported share',
        pair: pct(q.provenance.quoteUnsupportedShare),
        n: `${formatN(q.provenance.claims)} claims`,
      },
    ];
  });

  protected readonly outcomes = computed(() => {
    const o = this.readout().quality.outcomes;
    return OUTCOME_LABELS.filter(([key]) => o[key]).map(([key, label]) => ({
      label,
      pair: `${o[key][0].toLocaleString('en-US')} → ${o[key][1].toLocaleString('en-US')}`,
    }));
  });

  /** Counts always; a rate only at or above the floor (spec §10 Q2). */
  protected readonly thumbs = computed(() => {
    const t = this.readout().quality.thumbs;
    const side = (i: 0 | 1): string => {
      const counts = `${t.up[i]} up, ${t.down[i]} down`;
      const rate = t.downRate[i];
      const tail = rate === null ? `below floor (n=${t.n[i]})` : `down rate ${formatKpiValue(rate, 'percent')} (n=${t.n[i]})`;
      return `${counts}, ${tail}`;
    };
    const retries = `retries with correction ${t.retries[0]} → ${t.retries[1]}`;
    return `Baseline: ${side(0)}. Candidate: ${side(1)}. ${retries[0].toUpperCase()}${retries.slice(1)}.`;
  });

  protected readonly rangeRuns = computed<RangeRun[]>(() =>
    (this.range()?.units ?? [])
      .filter(u => u.eval.length > 0 || u.smoke.length > 0)
      .map(u => ({
        key: u.key,
        label: unitLabel(u.key),
        evalRuns: u.eval.length,
        evalFailed: u.eval.filter(e => e.gateFailed).length,
        smokeRuns: u.smoke.length,
        smokeFailed: u.smoke.filter(s => (s.fail ?? 0) > 0).length,
      })),
  );
}

function latest<T extends { startedAt?: string }>(runs: T[]): T[] {
  return [...runs].sort((a, b) => (b.startedAt ?? '').localeCompare(a.startedAt ?? '')).slice(0, RUNS_PER_SIDE);
}

function runTitle(run: { startedAt?: string; target?: string }, model?: string): string {
  const when = run.startedAt ? run.startedAt.slice(0, 16).replace('T', ' ') + ' UTC' : 'undated';
  return [when, model, run.target].filter(Boolean).join(' · ');
}

function evalView(run: LedgerEvalRun, index: number): EvalRunView {
  return {
    id: run.startedAt ?? String(index),
    title: runTitle(run, run.modelId),
    gateFailed: !!run.gateFailed,
    link: evaluationsLink(run),
    families: Object.entries(run.families ?? {})
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, f]) => ({
        name,
        n: f.n,
        base: f.base,
        run: f.run,
        gained: f.gained,
        lost: f.lost,
        p: Number.isFinite(f.p) ? f.p.toFixed(3) : '—',
        gate: f.gate,
      })),
  };
}

/** The Evaluations page filtered to this run's build, with the run open (eval-harness spec §16 D21). */
function evaluationsLink(run: LedgerEvalRun): Record<string, string> {
  const params: Record<string, string> = { track: run.track ?? 'untracked' };
  if (run.modelId) params['model'] = run.modelId;
  if (run.build) params['build'] = run.build;
  if (run.startedAt) params['run'] = run.startedAt;
  return params;
}

function smokeView(run: LedgerSmokeRun, index: number) {
  const counts = `${run.pass ?? 0} pass, ${run.fail ?? 0} fail, ${run.skip ?? 0} skip` +
    (run.observed ? `, ${run.observed} observed` : '');
  return {
    id: run.startedAt ?? String(index),
    title: runTitle(run),
    counts,
    failed: (run.fail ?? 0) > 0,
    firstToken: run.firstTokenMsMedian !== undefined ? `first token median ${formatKpiValue(run.firstTokenMsMedian, 'ms')}` : '',
    cache:
      run.secondTurnCacheRead !== undefined
        ? `second turn cache read ${run.secondTurnCacheRead.toLocaleString('en-US')} / write ${(run.secondTurnCacheWrite ?? 0).toLocaleString('en-US')}`
        : '',
  };
}

function pct(pair: LedgerPair<number | null>): string {
  return `${formatKpiValue(pair[0], 'percent')} → ${formatKpiValue(pair[1], 'percent')}`;
}
