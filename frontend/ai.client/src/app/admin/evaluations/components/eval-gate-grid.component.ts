import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { GateState, RunColumn, gateGrid } from '../evaluations.util';

const STATE_LABEL: Record<GateState, string> = {
  pass: 'passed',
  fail: 'failed',
  advisory: 'advisory',
  not_run: 'not run',
};

/** A glyph as well as a colour, so no state is told by colour alone. */
const STATE_GLYPH: Record<GateState, string> = {
  pass: '✓',
  fail: '✕',
  advisory: '~',
  not_run: '·',
};

const STATE_CLASS: Record<GateState, string> = {
  pass: 'bg-state-success-50 text-state-success-800 ring-1 ring-inset ring-state-success-200 dark:bg-state-success-900/30 dark:text-state-success-200 dark:ring-state-success-800',
  fail: 'bg-state-danger-50 text-state-danger-800 ring-1 ring-inset ring-state-danger-300 font-bold dark:bg-state-danger-900/40 dark:text-state-danger-200 dark:ring-state-danger-700',
  advisory: 'bg-gray-100 text-gray-700 ring-1 ring-inset ring-gray-200 dark:bg-gray-700/60 dark:text-gray-200 dark:ring-gray-600',
  not_run: 'text-gray-500 dark:text-gray-400',
};

/**
 * Gate history (eval-harness spec §16.3 item 2): one row per family, one cell
 * per run, oldest on the left. A failed gate is the one decision the page
 * exists for, so this is the first thing on it. Hovering a cell shows that
 * run's §8 row; choosing one opens the run's detail.
 */
@Component({
  selector: 'app-eval-gate-grid',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (columns().length === 0) {
      <p class="rounded-2xl border-2 border-dashed border-gray-200 p-6 text-center text-sm/6 text-gray-500 dark:border-gray-700 dark:text-gray-400">
        No runs for this model and track in the range.
      </p>
    } @else {
      <div class="overflow-x-auto rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800">
        <table class="min-w-full text-xs/5" data-testid="gate-grid">
          <caption class="sr-only">Gate verdict per family and run, oldest run first</caption>
          <thead>
            <tr>
              <th scope="col" class="sticky left-0 z-10 bg-white px-3 py-2 text-left font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">Family</th>
              @for (col of columns(); track col.id) {
                <th
                  scope="col"
                  class="px-0.5 py-2 text-center tabular-nums"
                  [class]="col.id === selectedRunId() ? 'font-semibold text-gray-900 dark:text-white' : 'font-normal text-gray-600 dark:text-gray-400'"
                  [attr.title]="col.when + ' · ' + col.build"
                >
                  <span class="sr-only">{{ col.when }}</span>
                  <span aria-hidden="true">{{ col.short }}</span>
                </th>
              }
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
            @for (row of rows(); track row.family) {
              <tr>
                <th scope="row" class="sticky left-0 z-10 bg-white px-3 py-1 text-left font-mono font-normal whitespace-nowrap text-gray-900 dark:bg-gray-800 dark:text-white">
                  {{ row.family }}
                </th>
                @for (cell of row.cells; track cell.runId) {
                  <td class="px-0.5 py-1 text-center">
                    <button
                      type="button"
                      [attr.data-state]="cell.state"
                      [attr.title]="cell.title"
                      [attr.aria-label]="cell.title"
                      [attr.aria-pressed]="cell.runId === selectedRunId()"
                      (click)="runSelect.emit(cell.runId)"
                      class="inline-flex size-7 items-center justify-center rounded-md text-sm/5 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500"
                      [class]="cellClass(cell.state, cell.runId)"
                    >
                      <span aria-hidden="true">{{ glyph[cell.state] }}</span>
                    </button>
                  </td>
                }
              </tr>
            }
          </tbody>
        </table>
      </div>
      <p class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs/5 text-gray-600 dark:text-gray-400">
        @for (s of legend; track s) {
          <span class="inline-flex items-center gap-1.5">
            <span class="inline-flex size-5 items-center justify-center rounded" [class]="stateClass[s]" aria-hidden="true">{{ glyph[s] }}</span>
            {{ label[s] }}
          </span>
        }
      </p>
    }
  `,
})
export class EvalGateGridComponent {
  /** One per run, oldest first. */
  columns = input.required<readonly RunColumn[]>();
  selectedRunId = input<string | null>(null);
  runSelect = output<string>();

  protected readonly rows = computed(() => gateGrid(this.columns()));
  protected readonly glyph = STATE_GLYPH;
  protected readonly label = STATE_LABEL;
  protected readonly stateClass = STATE_CLASS;
  protected readonly legend: readonly GateState[] = ['pass', 'fail', 'advisory', 'not_run'];

  protected cellClass(state: GateState, runId: string): string {
    const selected = runId === this.selectedRunId() ? ' outline-2 outline-offset-1 outline-gray-900 dark:outline-white' : '';
    return STATE_CLASS[state] + selected;
  }
}
