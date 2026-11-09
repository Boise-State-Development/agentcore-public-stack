import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { LedgerEvalRun } from '../../costs/models';
import {
  TRACK_LABELS,
  caseCount,
  failedCases,
  familyRows,
  formatCost,
  formatDuration,
  runBuild,
  runWhen,
  trackOf,
} from '../evaluations.util';

/**
 * Run detail (eval-harness spec §16.3 item 7): the run's §8 table as
 * `score` printed it, and its failed cases with their codes and names.
 *
 * The ledger row holds no answer, prompt or failure sentence (D7, D20), so the
 * page says where those live rather than implying it has them.
 */
@Component({
  selector: 'app-eval-run-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800" data-testid="run-detail">
      <div class="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
        <p class="text-sm/6 font-semibold text-gray-900 dark:text-white">
          {{ when() }} ·
          <span [class]="run().gateFailed ? 'text-state-danger-700 dark:text-state-danger-300' : 'text-state-success-700 dark:text-state-success-300'">
            gate {{ run().gateFailed ? 'failed' : 'passed' }}
          </span>
        </p>
        <dl class="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs/5 sm:grid-cols-4">
          @for (fact of facts(); track fact.label) {
            <div class="min-w-0">
              <dt class="text-gray-500 dark:text-gray-400">{{ fact.label }}</dt>
              <dd class="truncate font-mono text-gray-900 dark:text-white" [attr.title]="fact.value">{{ fact.value }}</dd>
            </div>
          }
        </dl>
      </div>

      <div class="space-y-6 px-6 py-5">
        <div class="overflow-x-auto">
          <table class="min-w-full text-xs/5">
            <caption class="pb-2 text-left text-sm/6 font-semibold text-gray-900 dark:text-white">Report (§8)</caption>
            <thead class="bg-gray-50 dark:bg-gray-900/40">
              <tr class="text-left font-medium text-gray-600 dark:text-gray-300">
                <th scope="col" class="px-3 py-1.5">family</th>
                <th scope="col" class="px-2 py-1.5 text-right">n</th>
                <th scope="col" class="px-2 py-1.5 text-right">base</th>
                <th scope="col" class="px-2 py-1.5 text-right">run</th>
                <th scope="col" class="px-2 py-1.5 text-right">flips(+/-)</th>
                <th scope="col" class="px-2 py-1.5 text-right">McNemar p</th>
                <th scope="col" class="px-3 py-1.5 text-right">gate</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100 font-mono tabular-nums dark:divide-gray-700/60">
              @for (f of families(); track f.family) {
                <tr class="text-gray-900 dark:text-white">
                  <th scope="row" class="px-3 py-1.5 text-left font-normal">
                    {{ f.family }}
                    @if (f.underFloor) {
                      <span class="ml-1 font-sans text-state-warning-800 dark:text-state-warning-200">(n={{ f.n }}, under 10)</span>
                    }
                  </th>
                  <td class="px-2 py-1.5 text-right">{{ f.n }}</td>
                  <td class="px-2 py-1.5 text-right">{{ f.base }}</td>
                  <td class="px-2 py-1.5 text-right">{{ f.run }}</td>
                  <td class="px-2 py-1.5 text-right">{{ f.flips }}</td>
                  <td class="px-2 py-1.5 text-right">{{ f.p }}</td>
                  <td class="px-3 py-1.5 text-right" [class]="f.gate === 'fail' ? 'font-semibold text-state-danger-700 dark:text-state-danger-300' : ''">{{ f.gate }}</td>
                </tr>
              }
            </tbody>
          </table>
        </div>

        <div>
          <h4 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Failed cases</h4>
          @if (!hasCases()) {
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">This run was published before per-case outcomes, so its row names no cases.</p>
          } @else if (failed().length === 0) {
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">Every case passed.</p>
          } @else {
            <ul role="list" class="mt-2 divide-y divide-gray-100 dark:divide-gray-700/60" data-testid="failed-cases">
              @for (c of failed(); track c.caseId) {
                <li class="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-1.5 text-xs/5">
                  <span class="font-mono text-gray-900 dark:text-white">{{ c.caseId }}</span>
                  <span class="tabular-nums text-gray-600 dark:text-gray-400">{{ c.passes }}/{{ c.samples }} samples</span>
                  @if (c.regressed) {
                    <span class="font-medium text-state-danger-700 dark:text-state-danger-300">baseline passed</span>
                  } @else if (c.newCase) {
                    <span class="text-gray-600 dark:text-gray-400">not in the baseline</span>
                  } @else {
                    <span class="text-gray-600 dark:text-gray-400">baseline failed too</span>
                  }
                  @for (code of c.codes; track code) {
                    <span class="rounded-md bg-gray-100 px-1.5 font-mono text-gray-800 dark:bg-gray-700 dark:text-gray-100">{{ code }}</span>
                  }
                  @for (name of c.names; track name) {
                    <span class="font-mono text-gray-600 dark:text-gray-300">{{ name }}</span>
                  }
                </li>
              }
            </ul>
          }
        </div>

        <p class="rounded-xl bg-gray-50 px-4 py-3 text-xs/5 text-gray-700 dark:bg-gray-900/40 dark:text-gray-300">
          The ledger holds ids, codes and counts only. The answers, prompts and failure sentences stay in the run
          directory (<code class="font-mono">eval_harness.py score --explain</code>), and the baseline's answer in
          the baseline file.
        </p>
      </div>
    </div>
  `,
})
export class EvalRunDetailComponent {
  run = input.required<LedgerEvalRun>();

  protected readonly when = computed(() => runWhen(this.run()));
  protected readonly families = computed(() => familyRows(this.run()));
  protected readonly failed = computed(() => failedCases(this.run()));
  protected readonly hasCases = computed(() => Object.keys(this.run().cases ?? {}).length > 0);

  protected readonly facts = computed(() => {
    const r = this.run();
    const facts = [
      { label: 'Track', value: TRACK_LABELS[trackOf(r)] },
      { label: 'Build', value: runBuild(r) },
      { label: 'Model', value: r.modelId ?? '—' },
      { label: 'k', value: r.k !== undefined ? String(r.k) : '—' },
      { label: 'Cases', value: String(caseCount(r)) },
      { label: 'Samples', value: r.samples !== undefined ? String(r.samples) : '—' },
      { label: 'Cost', value: formatCost(r.costUsd) },
      { label: 'Duration', value: formatDuration(r.durationS) },
      { label: 'Model calls', value: r.calls !== undefined ? String(r.calls) : '—' },
      { label: 'Target', value: r.target || '—' },
      { label: 'Commit', value: r.gitCommit ? r.gitCommit.slice(0, 12) : '—' },
    ];
    if (r.judgeModel) facts.push({ label: 'Judge', value: r.judgeModel });
    if (r.otherBuildCalls) facts.push({ label: 'Calls on another build', value: String(r.otherBuildCalls) });
    return facts;
  });
}
