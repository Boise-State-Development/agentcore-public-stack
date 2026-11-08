import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { LedgerReadout, ReadoutKpi, ReadoutLatencyMetric } from '../models';
import {
  DeltaTone,
  KPI_DEFS,
  KpiDef,
  deltaTone,
  formatDelta,
  formatKpiValue,
  formatN,
} from './release-ledger.util';

interface KpiCard {
  def: KpiDef;
  baseline: string;
  candidate: string;
  delta: string;
  tone: DeltaTone;
  n: string;
  /** Under its floor on a side that has data: the delta is "—" and the `n` explains why. */
  underFloor: boolean;
}

interface ModelRow {
  modelId: string;
  baseline: string;
  candidate: string;
  delta: string;
  tone: DeltaTone;
  n: string;
}

interface LatencyRow {
  label: string;
  p50: string;
  p90: string;
  n: string;
}

const TONE_CLASS: Record<DeltaTone, string> = {
  better: 'text-state-success-700 dark:text-state-success-300',
  worse: 'text-state-danger-700 dark:text-state-danger-300',
  neutral: 'text-gray-700 dark:text-gray-300',
  none: 'text-gray-500 dark:text-gray-400',
};

const TONE_TEXT: Record<DeltaTone, string> = {
  better: 'better',
  worse: 'worse',
  neutral: 'no direction',
  none: 'not comparable',
};

/**
 * The §6.2 KPIs as `baseline → candidate (Δ)`, cost per turn first and broken
 * out per model under it, every rate with its `n` (release-ledger spec §6.4
 * item 3). A delta with either side under its floor renders as "—" beside the
 * `n`, never as a number. The tone is written out for screen readers, and the
 * sign is in the text, so colour is never the only signal.
 */
@Component({
  selector: 'app-release-kpi-cards',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section aria-labelledby="release-kpis-heading" class="space-y-4">
      <h3 id="release-kpis-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
        KPIs
        <span class="ml-2 text-sm/6 font-normal text-gray-500 dark:text-gray-400">baseline → candidate (Δ)</span>
      </h3>

      @if (headline(); as card) {
        <div class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800">
          <div class="flex flex-col gap-2 px-6 py-4 sm:flex-row sm:items-baseline sm:justify-between">
            <div>
              <p class="text-sm/6 font-medium text-gray-600 dark:text-gray-300">{{ card.def.label }}</p>
              <p class="mt-1 text-2xl/8 font-semibold text-gray-900 tabular-nums dark:text-white">
                {{ card.baseline }} <span aria-hidden="true">→</span><span class="sr-only"> to</span> {{ card.candidate }}
                <span class="ml-1 text-base/7 font-medium" [class]="toneClass(card.tone)">
                  ({{ card.delta }}<span class="sr-only">, {{ toneText(card.tone) }}</span>)
                </span>
              </p>
            </div>
            <p class="text-xs/5 text-gray-500 tabular-nums dark:text-gray-400">
              {{ card.n }} {{ card.def.nOf }}@if (card.underFloor) {<span>, under floor</span>}
            </p>
          </div>
          @if (byModel().length > 0) {
            <div class="overflow-x-auto border-t border-gray-200 dark:border-gray-700">
              <table class="min-w-full text-sm/6">
                <caption class="sr-only">Cost per turn by model, baseline and candidate</caption>
                <thead>
                  <tr class="text-left text-xs/5 font-medium text-gray-500 dark:text-gray-400">
                    <th scope="col" class="px-6 py-2">Model</th>
                    <th scope="col" class="px-3 py-2 text-right">Baseline</th>
                    <th scope="col" class="px-3 py-2 text-right">Candidate</th>
                    <th scope="col" class="px-3 py-2 text-right">Δ</th>
                    <th scope="col" class="px-6 py-2 text-right">n (turns)</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
                  @for (row of byModel(); track row.modelId) {
                    <tr>
                      <th scope="row" class="max-w-xs truncate px-6 py-2 text-left font-mono text-xs/5 font-normal text-gray-700 dark:text-gray-300" [title]="row.modelId">
                        {{ row.modelId }}
                      </th>
                      <td class="px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.baseline }}</td>
                      <td class="px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.candidate }}</td>
                      <td class="px-3 py-2 text-right tabular-nums" [class]="toneClass(row.tone)">
                        {{ row.delta }}<span class="sr-only">, {{ toneText(row.tone) }}</span>
                      </td>
                      <td class="px-6 py-2 text-right text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">{{ row.n }}</td>
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          }
        </div>
      }

      <ul role="list" class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        @for (card of cards(); track card.def.key) {
          <li class="rounded-2xl border border-gray-200 bg-white px-5 py-4 shadow-xs dark:border-gray-700 dark:bg-gray-800" [attr.data-kpi]="card.def.key">
            <p class="text-sm/6 font-medium text-gray-600 dark:text-gray-300">{{ card.def.label }}</p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 tabular-nums dark:text-white">
              {{ card.baseline }} <span aria-hidden="true">→</span><span class="sr-only"> to</span> {{ card.candidate }}
            </p>
            <p class="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 text-xs/5 tabular-nums">
              <span class="font-medium" [class]="toneClass(card.tone)" data-testid="kpi-delta">
                Δ {{ card.delta }}<span class="sr-only">, {{ toneText(card.tone) }}</span>
              </span>
              <span class="text-gray-500 dark:text-gray-400" data-testid="kpi-n">
                {{ card.n }} {{ card.def.nOf }}@if (card.underFloor) {<span>, under floor</span>}
              </span>
            </p>
          </li>
        }
      </ul>

      @if (latency().length > 0) {
        <div class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800">
          <div class="px-6 pt-4">
            <p class="text-sm/6 font-medium text-gray-600 dark:text-gray-300">Time to first token</p>
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">
              From CloudWatch, by time rather than by build: a window with a deploy in it mixes builds.
            </p>
          </div>
          <div class="overflow-x-auto">
            <table class="mt-2 min-w-full text-sm/6">
              <caption class="sr-only">First-token latency percentiles, baseline to candidate</caption>
              <thead>
                <tr class="text-left text-xs/5 font-medium text-gray-500 dark:text-gray-400">
                  <th scope="col" class="px-6 py-2">Measure</th>
                  <th scope="col" class="px-3 py-2 text-right">p50</th>
                  <th scope="col" class="px-3 py-2 text-right">p90</th>
                  <th scope="col" class="px-6 py-2 text-right">n (samples)</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
                @for (row of latency(); track row.label) {
                  <tr>
                    <th scope="row" class="px-6 py-2 text-left font-normal text-gray-700 dark:text-gray-300">{{ row.label }}</th>
                    <td class="px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.p50 }}</td>
                    <td class="px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.p90 }}</td>
                    <td class="px-6 py-2 text-right text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">{{ row.n }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        </div>
      }
    </section>
  `,
})
export class ReleaseKpiCardsComponent {
  readout = input.required<LedgerReadout>();

  private readonly allCards = computed<KpiCard[]>(() => {
    const kpis = this.readout().kpis;
    return KPI_DEFS.filter(def => kpis[def.key]).map(def => card(def, kpis[def.key]));
  });

  protected readonly headline = computed(() => this.allCards().find(c => c.def.key === 'costPerTurn') ?? null);
  protected readonly cards = computed(() => this.allCards().filter(c => c.def.key !== 'costPerTurn'));

  protected readonly byModel = computed<ModelRow[]>(() => {
    const byModel = this.readout().kpis['costPerTurn']?.byModel ?? {};
    return Object.entries(byModel)
      .map(([modelId, k]) => ({
        modelId,
        baseline: formatKpiValue(k.baseline, 'usd'),
        candidate: formatKpiValue(k.candidate, 'usd'),
        delta: formatDelta(k.delta),
        tone: deltaTone(k.delta, 'lower'),
        n: formatN(k.n),
        weight: k.n[1] + k.n[0],
      }))
      .sort((a, b) => b.weight - a.weight || a.modelId.localeCompare(b.modelId));
  });

  protected readonly latency = computed<LatencyRow[]>(() => {
    const l = this.readout().latency;
    if (!l?.available) return [];
    const rows: [string, ReadoutLatencyMetric | null | undefined][] = [
      ['Server first token', l.serverFirstTokenMs],
      ['Client first token', l.clientFirstTokenMs],
      ['Probe, cold', l.probeColdFirstTokenMs],
      ['Probe, follow-up', l.probeFollowUpFirstTokenMs],
    ];
    return rows
      .filter((r): r is [string, ReadoutLatencyMetric] => !!r[1])
      .map(([label, m]) => ({
        label,
        p50: `${formatKpiValue(m.p50[0], 'ms')} → ${formatKpiValue(m.p50[1], 'ms')}`,
        p90: `${formatKpiValue(m.p90[0], 'ms')} → ${formatKpiValue(m.p90[1], 'ms')}`,
        n: formatN(m.n),
      }));
  });

  protected toneClass(tone: DeltaTone): string {
    return TONE_CLASS[tone];
  }

  protected toneText(tone: DeltaTone): string {
    return TONE_TEXT[tone];
  }
}

function card(def: KpiDef, k: ReadoutKpi): KpiCard {
  // A side with data whose value is still null is under its floor; a side
  // with no denominator at all (n 0) has nothing to show, which is not the same.
  const underFloor = (k.baseline === null && k.n[0] > 0) || (k.candidate === null && k.n[1] > 0);
  return {
    def,
    baseline: formatKpiValue(k.baseline, def.format),
    candidate: formatKpiValue(k.candidate, def.format),
    delta: formatDelta(k.delta),
    tone: deltaTone(k.delta, def.better),
    n: formatN(k.n),
    underFloor,
  };
}
