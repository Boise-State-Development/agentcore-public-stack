import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { LedgerPair, ReadoutCallKind } from '../models';
import { KpiFormat, callKindLabel, formatKpiValue, formatN } from './release-ledger.util';

interface CallKindRow {
  kind: string;
  label: string;
  calls: string;
  cost: string;
  firstByte: string;
  n: string;
  turnKpis: boolean;
}

/**
 * Calls, cost and mean model first byte per call kind (release-ledger spec
 * §6.4 item 7, §5.2.1): `chat` is what the turn KPIs read, and every other kind (workflow
 * prompt and checklist steps, handoff briefs, voice, api-converse,
 * extraction) is a call that is not a turn, kept visible here instead of
 * inside cost per turn. Pairs as the readout gives them; a side whose rows
 * predate the chat split reads "—", never zero.
 */
@Component({
  selector: 'app-release-call-kinds-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section
      aria-labelledby="release-call-kinds-heading"
      class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
    >
      <div class="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
        <h3 id="release-call-kinds-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Calls by kind</h3>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Baseline → candidate. Cost per turn and mean model first byte read chat calls only; the other kinds are calls, not turns.
        </p>
      </div>
      @if (rows().length === 0) {
        <p class="px-6 py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">No calls in either window.</p>
      } @else {
        <div class="overflow-x-auto">
          <table class="min-w-full text-sm/6">
            <caption class="sr-only">Calls, cost and mean model first byte by call kind, baseline to candidate</caption>
            <thead class="bg-gray-50 dark:bg-gray-900/40">
              <tr class="text-left text-xs/5 font-medium text-gray-600 dark:text-gray-300">
                <th scope="col" class="px-6 py-2">Kind</th>
                <th scope="col" class="px-3 py-2 text-right">Calls</th>
                <th scope="col" class="px-3 py-2 text-right">Cost</th>
                <th scope="col" class="px-3 py-2 text-right">Mean model first byte</th>
                <th scope="col" class="px-6 py-2 text-right">n (first bytes)</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
              @for (row of rows(); track row.kind) {
                <tr [attr.data-kind]="row.kind">
                  <th scope="row" class="px-6 py-2 text-left font-normal text-gray-800 dark:text-gray-200">
                    {{ row.label }}
                    @if (row.turnKpis) {
                      <span class="ml-1 text-xs/5 text-gray-500 dark:text-gray-400">(turn KPIs)</span>
                    }
                  </th>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.calls }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.cost }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.firstByte }}</td>
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
export class ReleaseCallKindsTableComponent {
  kinds = input.required<ReadoutCallKind[]>();

  protected readonly rows = computed<CallKindRow[]>(() =>
    this.kinds().map(k => ({
      kind: k.kind,
      label: callKindLabel(k.kind),
      calls: pair(k.calls, 'number'),
      cost: pair(k.cost, 'usd'),
      firstByte: pair(k.meanFirstTokenMs, 'ms'),
      n: formatN(k.firstTokenN),
      turnKpis: k.kind === 'chat',
    })),
  );
}

function pair(values: LedgerPair<number | null>, format: KpiFormat): string {
  return `${formatKpiValue(values[0], format)} → ${formatKpiValue(values[1], format)}`;
}
