import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { CaseCell, NOISY_FLIPS, NOISY_WINDOW, RunColumn, flipMatrix } from '../evaluations.util';

/**
 * Case flip matrix (eval-harness spec §16.3 item 4): for one family, one row
 * per case and one cell per run. A case that failed against a passing baseline
 * is marked; one that flips at least three times in ten runs on one build is
 * tagged noisy, which makes it a case to fix rather than a regression.
 *
 * Only runs published with per-case outcomes (EH-9) have cells; an older run
 * shows as absent for every case.
 */
@Component({
  selector: 'app-eval-flip-matrix',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (rows().length === 0) {
      <p class="rounded-2xl border-2 border-dashed border-gray-200 p-6 text-center text-sm/6 text-gray-500 dark:border-gray-700 dark:text-gray-400">
        No per-case outcomes for {{ family() }} in these runs. Rows published before per-case outcomes carry none.
      </p>
    } @else {
      <div class="overflow-x-auto rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800">
        <table class="min-w-full text-xs/5" data-testid="flip-matrix">
          <caption class="sr-only">Outcome per case of {{ family() }} and run, oldest run first</caption>
          <thead>
            <tr>
              <th scope="col" class="sticky left-0 z-10 bg-white px-3 py-2 text-left font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">Case</th>
              @for (col of columns(); track col.id) {
                <th scope="col" class="px-0.5 py-2 text-center font-normal tabular-nums text-gray-600 dark:text-gray-400" [attr.title]="col.when + ' · ' + col.build">
                  <span class="sr-only">{{ col.when }}</span>
                  <span aria-hidden="true">{{ col.short }}</span>
                </th>
              }
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100 dark:divide-gray-700/60">
            @for (row of rows(); track row.caseId) {
              <tr [attr.data-case]="row.caseId">
                <th scope="row" class="sticky left-0 z-10 bg-white px-3 py-1 text-left font-normal whitespace-nowrap dark:bg-gray-800">
                  <span class="font-mono text-gray-900 dark:text-white" [attr.title]="row.caseId">{{ row.label }}</span>
                  @if (row.noisy) {
                    <span
                      class="ml-2 rounded-full bg-state-warning-50 px-2 py-0.5 text-xs/5 font-medium text-state-warning-800 ring-1 ring-inset ring-state-warning-200 dark:bg-state-warning-900/30 dark:text-state-warning-200 dark:ring-state-warning-800"
                      data-testid="noisy"
                      [attr.title]="noisyTitle"
                    >noisy</span>
                  }
                </th>
                @for (cell of row.cells; track cell.runId) {
                  <td class="px-0.5 py-1 text-center">
                    <button
                      type="button"
                      [attr.data-state]="cell.state"
                      [attr.data-regressed]="cell.regressed || null"
                      [attr.title]="cell.title"
                      [attr.aria-label]="cell.title"
                      (click)="runSelect.emit(cell.runId)"
                      class="inline-flex size-6 items-center justify-center rounded text-xs/5 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500"
                      [class]="cellClass(cell)"
                    >
                      <span aria-hidden="true">{{ glyph(cell) }}</span>
                    </button>
                  </td>
                }
              </tr>
            }
          </tbody>
        </table>
      </div>
      <p class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs/5 text-gray-600 dark:text-gray-400">
        <span class="inline-flex items-center gap-1.5"><span class="inline-flex size-5 items-center justify-center rounded" [class]="passClass" aria-hidden="true">✓</span>pass</span>
        <span class="inline-flex items-center gap-1.5"><span class="inline-flex size-5 items-center justify-center rounded" [class]="failClass" aria-hidden="true">✕</span>fail</span>
        <span class="inline-flex items-center gap-1.5"><span class="inline-flex size-5 items-center justify-center rounded" [class]="regressedClass" aria-hidden="true">!</span>failed, baseline passed</span>
        <span class="inline-flex items-center gap-1.5"><span class="inline-flex size-5 items-center justify-center text-gray-500 dark:text-gray-400" aria-hidden="true">·</span>not in the run</span>
        <span>{{ noisyTitle }}</span>
      </p>
    }
  `,
})
export class EvalFlipMatrixComponent {
  columns = input.required<readonly RunColumn[]>();
  family = input.required<string>();
  runSelect = output<string>();

  protected readonly rows = computed(() => flipMatrix(this.columns(), this.family()));
  protected readonly noisyTitle = `Noisy: flips at least ${NOISY_FLIPS} times in ${NOISY_WINDOW} runs on one build.`;

  protected readonly passClass =
    'bg-state-success-50 text-state-success-800 ring-1 ring-inset ring-state-success-200 dark:bg-state-success-900/30 dark:text-state-success-200 dark:ring-state-success-800';
  protected readonly failClass =
    'bg-state-danger-50 text-state-danger-800 ring-1 ring-inset ring-state-danger-300 dark:bg-state-danger-900/40 dark:text-state-danger-200 dark:ring-state-danger-700';
  protected readonly regressedClass =
    'bg-state-danger-600 font-bold text-white ring-1 ring-inset ring-state-danger-700 dark:bg-state-danger-500 dark:text-gray-950';

  protected cellClass(cell: CaseCell): string {
    if (cell.state === 'pass') return this.passClass;
    if (cell.state === 'fail') return cell.regressed ? this.regressedClass : this.failClass;
    return 'text-gray-500 dark:text-gray-400';
  }

  protected glyph(cell: CaseCell): string {
    if (cell.state === 'pass') return '✓';
    if (cell.state === 'fail') return cell.regressed ? '!' : '✕';
    return '·';
  }
}
