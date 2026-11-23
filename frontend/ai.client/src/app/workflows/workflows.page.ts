import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronRight, heroExclamationTriangle, heroPlus, heroRectangleGroup, heroSquare2Stack } from '@ng-icons/heroicons/outline';

import { PROPOSAL_REVIEW_TEMPLATE } from './workflow-draft';
import { WorkflowSummary, designerLink } from './workflow.models';
import { WorkflowsApiService } from '../services/workflows/workflows-api.service';

interface ListRow {
  summary: WorkflowSummary;
  title: string;
  meta: string;
  link: string[];
}

/**
 * The Workflows page (docs/specs/workflow-designer.md §8.1, D5): your workflows first, then any
 * you can use that aren't yours. Today the second list comes only from role grants: a workflow is
 * a skill, and a skill isn't shared person to person, so this page has no Share.
 *
 * New starts a blank draft or the one template; nothing is stored until the designer saves.
 *
 * The list is the service's, kept for the tab: coming back shows it at once and refreshes it
 * behind the copy shown, so only the first visit has a "Loading" line.
 */
@Component({
  selector: 'app-workflows-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, NgTemplateOutlet, RouterLink],
  providers: [provideIcons({ heroChevronRight, heroExclamationTriangle, heroPlus, heroRectangleGroup, heroSquare2Stack })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-10 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 class="text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl dark:text-white">Workflows</h1>
            <p class="mt-1.5 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
              Several Agents working on one request, from one approval. Design a workflow once and start it whenever you need it.
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <a
              routerLink="/workflows/new"
              [queryParams]="{ template: template }"
              class="inline-flex items-center gap-2 rounded-2xl border border-gray-200 bg-white px-3.5 py-2 text-sm/6 font-medium text-gray-700 shadow-xs transition hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              <ng-icon name="heroSquare2Stack" class="size-4" aria-hidden="true" />
              From the proposal review template
            </a>
            <a
              routerLink="/workflows/new"
              class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white shadow-xs transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark"
            >
              <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
              New workflow
            </a>
          </div>
        </div>

        @if (!list.hasValue() && list.isLoading()) {
          <p class="sr-only" role="status">Loading workflows…</p>
          <!-- Rows shaped like the real ones (same height and spacing), so nothing moves when the list arrives. -->
          <div aria-hidden="true" class="animate-pulse motion-reduce:animate-none">
            <div class="flex h-7 items-center">
              <div class="h-4 w-16 rounded-sm bg-gray-200 dark:bg-gray-700"></div>
            </div>
            <ul class="mt-1">
              @for (width of skeletonWidths; track $index) {
                <li class="flex items-center gap-4 border-b border-gray-100 py-3.5 last:border-0 dark:border-white/5">
                  <div class="size-9 shrink-0 rounded-xl bg-gray-100 dark:bg-gray-700/50"></div>
                  <div class="min-w-0 flex-1">
                    <div class="flex h-6 items-center"><div class="h-3.5 rounded-sm bg-gray-200 dark:bg-gray-700" [class]="width"></div></div>
                    <div class="flex h-5 items-center"><div class="h-3 w-28 rounded-sm bg-gray-100 dark:bg-gray-700/50"></div></div>
                  </div>
                </li>
              }
            </ul>
          </div>
        } @else if (list.error()) {
          <div role="alert" class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-800/60 dark:bg-state-danger-900/20 dark:text-state-danger-300">
            Your workflows couldn’t be loaded.
            <button type="button" (click)="list.reload()" class="ml-2 font-medium underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark">Try again</button>
          </div>
        } @else if (!yours().length && !others().length) {
          <div class="rounded-2xl border border-dashed border-gray-300 px-6 py-12 text-center dark:border-gray-600">
            <ng-icon name="heroRectangleGroup" class="mx-auto size-8 text-gray-500 dark:text-gray-400" aria-hidden="true" />
            <h2 class="mt-3 text-base/7 font-semibold text-gray-900 dark:text-white">No workflows yet</h2>
            <p class="mx-auto mt-1 max-w-md text-sm/6 text-gray-600 dark:text-gray-400">
              Start from blank or the template here, or hand off to several Agents in a conversation and choose Save as workflow on its run.
            </p>
          </div>
        } @else {
          @if (yours().length) {
            <section aria-labelledby="wf-yours">
              <h2 id="wf-yours" class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white">
                Yours
                <span class="font-mono text-xs/5 font-normal text-gray-600 tabular-nums dark:text-gray-400">{{ yours().length }}</span>
              </h2>
              <ul role="list" class="mt-1">
                @for (row of yours(); track row.summary.skillId + '/' + row.summary.name) {
                  <ng-container *ngTemplateOutlet="rowTpl; context: { $implicit: row }" />
                }
              </ul>
            </section>
          }
          @if (others().length) {
            <section aria-labelledby="wf-others" [class.mt-10]="yours().length > 0">
              <h2 id="wf-others" class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white">
                You can use
                <span class="font-mono text-xs/5 font-normal text-gray-600 tabular-nums dark:text-gray-400">{{ others().length }}</span>
              </h2>
              <p class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">Granted to a role you have. You can open and start these, not change them.</p>
              <ul role="list" class="mt-1">
                @for (row of others(); track row.summary.skillId + '/' + row.summary.name) {
                  <ng-container *ngTemplateOutlet="rowTpl; context: { $implicit: row }" />
                }
              </ul>
            </section>
          }
        }
      </div>
    </div>

    <ng-template #rowTpl let-row>
      <li class="relative flex items-center gap-4 border-b border-gray-100 py-3.5 last:border-0 dark:border-white/5">
        <span class="grid size-9 shrink-0 place-items-center rounded-xl bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50" aria-hidden="true">
          <ng-icon name="heroRectangleGroup" class="size-5" />
        </span>
        <div class="min-w-0 flex-1">
          <h3 class="truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
            <a [routerLink]="row.link" class="after:absolute after:inset-0 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:focus-visible:outline-primary-accessible-dark">{{ row.title }}</a>
          </h3>
          @if (row.summary.problem) {
            <p class="flex items-center gap-1 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
              <ng-icon name="heroExclamationTriangle" class="size-3.5 shrink-0" aria-hidden="true" />
              <span class="truncate">Can’t be read: {{ row.summary.problem }}</span>
            </p>
          } @else {
            <p class="truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ row.meta }}</p>
          }
        </div>
        <span class="relative z-10 hidden shrink-0 items-center gap-1 text-sm/6 font-medium text-primary-accessible sm:flex dark:text-primary-accessible-dark" aria-hidden="true">
          Open
          <ng-icon name="heroChevronRight" class="size-4" />
        </span>
      </li>
    </ng-template>
  `,
})
export class WorkflowsPage {
  private readonly workflows = inject(WorkflowsApiService);

  protected readonly template = PROPOSAL_REVIEW_TEMPLATE;

  /** Title widths for the first-load skeleton's rows, varied so it reads as a list, not a block. */
  protected readonly skeletonWidths = ['w-2/5', 'w-1/3', 'w-1/2'];

  protected readonly list = this.workflows.workflows;

  constructor() {
    this.workflows.openList();
  }

  private readonly rows = computed<ListRow[]>(() =>
    (this.list.hasValue() ? this.list.value() : []).map((summary) => {
      const steps = summary.stepCount ?? 0;
      const parts = [`${steps} ${steps === 1 ? 'step' : 'steps'}`];
      if (summary.version) parts.push(`version ${summary.version}`);
      if (!summary.designerMade) parts.push(`in the skill ${summary.skillName}`);
      return { summary, title: summary.title || summary.name, meta: parts.join(' · '), link: designerLink(summary) };
    }),
  );

  protected readonly yours = computed(() => this.rows().filter((row) => row.summary.isOwned));
  protected readonly others = computed(() => this.rows().filter((row) => !row.summary.isOwned));
}
