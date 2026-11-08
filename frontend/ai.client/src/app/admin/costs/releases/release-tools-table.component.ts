import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { LedgerPair, ReadoutTool } from '../models';
import { KpiFormat, formatKpiValue, sortTools } from './release-ledger.util';

interface ToolRow {
  toolId: string;
  calls: string;
  errorRate: string;
  payload: string;
  latency: string;
  estTokens: string;
  estUsd: string;
  specTokens: string;
  unmeasured: boolean;
}

/** Census columns the tools route reports as not recorded yet, in words. */
const NOT_MEASURED_LABELS: Record<string, string> = {
  awaitingUser: 'awaiting-user share',
  callsWithUi: 'calls with an MCP App UI',
};

/**
 * The tools table (release-ledger spec §6.4 item 5): sorted by estimated input
 * dollars, each measure as `baseline → candidate`, with the spec tokens from
 * the committed CountTokens measurement beside the payload estimate, never
 * added to it (D4). The readout gives pairs here, not deltas, and the tab does
 * not compute one of its own (D10).
 */
@Component({
  selector: 'app-release-tools-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section
      aria-labelledby="release-tools-heading"
      class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
    >
      <div class="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
        <h3 id="release-tools-heading" class="text-lg/7 font-semibold text-gray-900 dark:text-white">Tools</h3>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Baseline → candidate. Input tokens and dollars are estimates (result characters ÷ 4 at the call's input rate);
          spec tokens are the schema's cost in every turn's tool config, measured separately.
        </p>
      </div>
      @if (rows().length === 0) {
        <p class="px-6 py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">No tool calls in either window.</p>
      } @else {
        <div class="overflow-x-auto">
          <table class="min-w-full text-sm/6">
            <caption class="sr-only">Tools by estimated input dollars, baseline to candidate</caption>
            <thead class="bg-gray-50 dark:bg-gray-900/40">
              <tr class="text-left text-xs/5 font-medium text-gray-600 dark:text-gray-300">
                <th scope="col" class="px-6 py-2">Tool</th>
                <th scope="col" class="px-3 py-2 text-right">Calls</th>
                <th scope="col" class="px-3 py-2 text-right">Error rate</th>
                <th scope="col" class="px-3 py-2 text-right">Payload / call</th>
                <th scope="col" class="px-3 py-2 text-right">Mean latency</th>
                <th scope="col" class="px-3 py-2 text-right">Est. input tokens</th>
                <th scope="col" class="px-3 py-2 text-right">Est. input $</th>
                <th scope="col" class="px-6 py-2 text-right">Spec tokens</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
              @for (row of rows(); track row.toolId) {
                <tr>
                  <th scope="row" class="px-6 py-2 text-left font-mono text-xs/5 font-normal text-gray-800 dark:text-gray-200">
                    {{ row.toolId }}
                    @if (row.unmeasured) {
                      <span class="ml-1 font-sans text-gray-500 dark:text-gray-400">(payload not measured)</span>
                    }
                  </th>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.calls }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.errorRate }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.payload }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.latency }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.estTokens }}</td>
                  <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-900 dark:text-white">{{ row.estUsd }}</td>
                  <td class="whitespace-nowrap px-6 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ row.specTokens }}</td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }
      @if (notMeasuredText()) {
        <p class="border-t border-gray-200 px-6 py-3 text-xs/5 text-gray-500 dark:border-gray-700 dark:text-gray-400">
          Not recorded by the tool census yet: {{ notMeasuredText() }}.
        </p>
      }
    </section>
  `,
})
export class ReleaseToolsTableComponent {
  tools = input.required<ReadoutTool[]>();
  notMeasured = input<string[]>([]);

  protected readonly rows = computed<ToolRow[]>(() =>
    sortTools(this.tools()).map(t => ({
      toolId: t.toolId,
      calls: pair(t.calls, 'number'),
      errorRate: pair(t.errorRate, 'percent'),
      payload: pair(measured(t, t.resultCharsPerCall), 'chars'),
      latency: pair(t.meanDurationMs, 'ms'),
      estTokens: pair(measured(t, t.estInputTokens), 'number'),
      estUsd: pair(measured(t, t.estInputUsd), 'usd'),
      specTokens: t.specTokens === null ? '—' : t.specTokens.toLocaleString('en-US'),
      unmeasured: t.payloadMeasured.includes(false),
    })),
  );

  protected readonly notMeasuredText = computed(() =>
    this.notMeasured()
      .map(f => NOT_MEASURED_LABELS[f] ?? f)
      .join(', '),
  );
}

/**
 * A window whose rows predate the census's payload fields reads 0 for every
 * payload measure; that is "not measured", not "nothing", so it prints as "—".
 */
function measured(tool: ReadoutTool, values: LedgerPair<number | null>): LedgerPair<number | null> {
  return [
    tool.payloadMeasured[0] === false ? null : values[0],
    tool.payloadMeasured[1] === false ? null : values[1],
  ];
}

function pair(values: LedgerPair<number | null>, format: KpiFormat): string {
  return `${formatKpiValue(values[0], format)} → ${formatKpiValue(values[1], format)}`;
}
