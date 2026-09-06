import {
  Component,
  ChangeDetectionStrategy,
  input,
  output,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { TopSessionCost } from '../models';

/**
 * Most expensive conversations for a period.
 *
 * The support-side counterpart to the per-session quota notice the user now
 * sees (#833 PR-5): a single runaway thread can spend most of a monthly
 * budget while the per-user numbers still look ordinary, and until this
 * existed the only way to find one was to already know its session ID.
 *
 * Rows are cost-sorted server-side. `partialMissUsd` is surfaced next to the
 * dollars because a conversation that is expensive from prompt-cache waste
 * is a platform problem, not a heavy user.
 */
@Component({
  selector: 'app-top-sessions-table',
  imports: [DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="bg-white dark:bg-gray-800 rounded-lg shadow-xs border border-gray-200 dark:border-gray-700"
    >
      <!-- Header -->
      <div
        class="flex flex-col gap-3 px-6 py-4 border-b border-gray-200 sm:flex-row sm:items-center sm:justify-between dark:border-gray-700"
      >
        <div>
          <h3 class="text-lg font-semibold text-gray-900 dark:text-white">
            Most Expensive Conversations
          </h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Lifetime cost per session, for sessions active in this period.
          </p>
        </div>
        <button
          type="button"
          (click)="load.emit()"
          [disabled]="loading()"
          class="shrink-0 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {{ loading() ? 'Loading…' : hasLoaded() ? 'Refresh' : 'Load' }}
        </button>
      </div>

      @if (sessions().length > 0) {
        <div class="overflow-x-auto">
          <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead class="bg-gray-50 dark:bg-gray-900">
              <tr>
                <th
                  scope="col"
                  class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
                >
                  Conversation
                </th>
                <th
                  scope="col"
                  class="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
                >
                  Cost
                </th>
                <th
                  scope="col"
                  class="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
                >
                  Share of user
                </th>
                <th
                  scope="col"
                  class="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
                >
                  Cache waste
                </th>
                <th
                  scope="col"
                  class="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider"
                >
                  Last active
                </th>
              </tr>
            </thead>
            <tbody
              class="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-800"
            >
              @for (session of sessions(); track session.sessionId) {
                <tr
                  class="cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
                  (click)="sessionClick.emit(session.sessionId)"
                >
                  <td class="px-4 py-3">
                    <div class="text-sm font-medium text-gray-900 dark:text-white">
                      {{ session.title || 'Untitled conversation' }}
                    </div>
                    <div class="font-mono text-xs text-gray-500 dark:text-gray-400">
                      {{ session.sessionId }}
                    </div>
                  </td>
                  <td
                    class="px-4 py-3 text-right text-sm font-semibold text-gray-900 tabular-nums dark:text-white"
                  >
                    {{ '$' + (session.totalCost | number: '1.2-2') }}
                  </td>
                  <td
                    class="px-4 py-3 text-right text-sm text-gray-700 tabular-nums dark:text-gray-300"
                  >
                    @if (session.shareOfUserPeriod !== null && session.shareOfUserPeriod !== undefined) {
                      {{ (session.shareOfUserPeriod | number: '1.0-0') + '%' }}
                    } @else {
                      —
                    }
                  </td>
                  <td class="px-4 py-3 text-right text-sm tabular-nums">
                    @if (session.partialMissUsd) {
                      <span class="font-medium text-metric-cost-700 dark:text-metric-cost-300">
                        {{ '$' + (session.partialMissUsd | number: '1.2-2') }}
                      </span>
                    } @else {
                      <span class="text-gray-400 dark:text-gray-500">—</span>
                    }
                  </td>
                  <td
                    class="px-4 py-3 text-right text-sm text-gray-500 dark:text-gray-400"
                  >
                    {{ formatDate(session.lastMessageAt) }}
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>

        @if (truncated()) {
          <div
            class="px-6 py-3 text-xs text-gray-500 border-t border-gray-200 dark:border-gray-700 dark:text-gray-400"
          >
            More users had spend this period than were scanned — raise the
            scan depth to widen the search.
          </div>
        }
      } @else if (hasLoaded() && !loading()) {
        <div class="px-6 py-8 text-center text-sm text-gray-500 dark:text-gray-400">
          No sessions with recorded cost in this period.
        </div>
      } @else if (!hasLoaded()) {
        <div class="px-6 py-8 text-center text-sm text-gray-500 dark:text-gray-400">
          Load to scan this period's highest-cost users for runaway
          conversations.
        </div>
      }
    </div>
  `,
})
export class TopSessionsTableComponent {
  sessions = input<TopSessionCost[]>([]);
  loading = input<boolean>(false);
  truncated = input<boolean>(false);
  hasLoaded = input<boolean>(false);

  load = output<void>();
  sessionClick = output<string>();

  protected formatDate(value: string | null | undefined): string {
    if (!value) return '—';

    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '—';

    return parsed.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  }
}
