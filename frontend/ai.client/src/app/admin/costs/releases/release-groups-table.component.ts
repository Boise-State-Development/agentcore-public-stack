import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { ReadoutGroup } from '../models';
import { DeltaTone, deltaTone, formatDelta, formatKpiValue, formatN, topGroups } from './release-ledger.util';

interface GroupRow {
  id: string;
  turns: string;
  cost: string;
  costPerTurn: string;
  costPerTurnDelta: string;
  costTone: DeltaTone;
  wastePerTurn: string;
  wastePerTurnDelta: string;
  wasteTone: DeltaTone;
  toolErrorRate: string;
  n: string;
}

const TONE_CLASS: Record<DeltaTone, string> = {
  better: 'text-state-success-700 dark:text-state-success-300',
  worse: 'text-state-danger-700 dark:text-state-danger-300',
  neutral: 'text-gray-700 dark:text-gray-300',
  none: 'text-gray-500 dark:text-gray-400',
};

/**
 * Agents or projects (release-ledger spec §6.4 item 6): cost and waste per
 * turn as `baseline → candidate (Δ)`, the top twenty by candidate cost. Ids
 * only, as the session profile already shows them (D7).
 */
@Component({
  selector: 'app-release-groups-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section
      [attr.aria-labelledby]="headingId()"
      class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
    >
      <div class="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
        <h3 [id]="headingId()" class="text-lg/7 font-semibold text-gray-900 dark:text-white">{{ heading() }}</h3>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">Top twenty by candidate cost. Baseline → candidate.</p>
      </div>
      @if (rows().length === 0) {
        <p class="px-6 py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">{{ emptyText() }}</p>
      } @else {
        <div class="overflow-x-auto">
          <table class="min-w-full text-sm/6">
            <caption class="sr-only">{{ heading() }}: cost and waste per turn, baseline to candidate</caption>
            <thead class="bg-gray-50 dark:bg-gray-900/40">
              <tr class="text-left text-xs/5 font-medium text-gray-600 dark:text-gray-300">
                <th scope="col" class="px-6 py-2">{{ idLabel() }}</th>
                <th scope="col" class="px-3 py-2 text-right">Turns</th>
                <th scope="col" class="px-3 py-2 text-right">Cost</th>
                <th scope="col" class="px-3 py-2 text-right">Cost / turn (Δ)</th>
                <th scope="col" class="px-3 py-2 text-right">Waste / turn (Δ)</th>
                <th scope="col" class="px-3 py-2 text-right">Tool error rate</th>
                <th scope="col" class="px-6 py-2 text-right">n (turns)</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
              @for (row of rows(); track row.id) {
                <tr>
                  <th scope="row" class="max-w-[14rem] truncate px-6 py-2 text-left font-mono text-xs/5 font-normal text-gray-800 dark:text-gray-200" [title]="row.id">
                    {{ row.id }}
                  </th>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.turns }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.cost }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">
                    {{ row.costPerTurn }} <span [class]="toneClass(row.costTone)">({{ row.costPerTurnDelta }})</span>
                  </td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">
                    {{ row.wastePerTurn }} <span [class]="toneClass(row.wasteTone)">({{ row.wastePerTurnDelta }})</span>
                  </td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.toolErrorRate }}</td>
                  <td class="whitespace-nowrap px-6 py-2 text-right text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">{{ row.n }}</td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }
    </section>
  `,
})
export class ReleaseGroupsTableComponent {
  heading = input.required<string>();
  idLabel = input.required<string>();
  groups = input.required<ReadoutGroup[]>();
  emptyText = input('Nothing in either window.');

  protected readonly headingId = computed(() => `release-${this.heading().toLowerCase().replace(/\W+/g, '-')}-heading`);

  protected readonly rows = computed<GroupRow[]>(() =>
    topGroups(this.groups()).map(g => ({
      id: g.id,
      turns: `${g.turns[0].toLocaleString('en-US')} → ${g.turns[1].toLocaleString('en-US')}`,
      cost: `${formatKpiValue(g.cost[0], 'usd')} → ${formatKpiValue(g.cost[1], 'usd')}`,
      costPerTurn: `${formatKpiValue(g.costPerTurn.baseline, 'usd')} → ${formatKpiValue(g.costPerTurn.candidate, 'usd')}`,
      costPerTurnDelta: formatDelta(g.costPerTurn.delta),
      costTone: deltaTone(g.costPerTurn.delta, 'lower'),
      wastePerTurn: `${formatKpiValue(g.wastePerTurn.baseline, 'usd')} → ${formatKpiValue(g.wastePerTurn.candidate, 'usd')}`,
      wastePerTurnDelta: formatDelta(g.wastePerTurn.delta),
      wasteTone: deltaTone(g.wastePerTurn.delta, 'lower'),
      toolErrorRate: `${formatKpiValue(g.toolErrorRate[0], 'percent')} → ${formatKpiValue(g.toolErrorRate[1], 'percent')}`,
      n: formatN(g.costPerTurn.n),
    })),
  );

  protected toneClass(tone: DeltaTone): string {
    return TONE_CLASS[tone];
  }
}
