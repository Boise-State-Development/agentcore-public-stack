import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { AdminCostHttpService } from '../services/admin-cost-http.service';
import { UserSessionSummary, UserSessionsSort } from '../models';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import {
  formatTokensShort,
  severityDotClass,
  shortSessionId,
} from '../pages/session-profile.util';

/**
 * A user's conversations, content-free, for the admin user page.
 *
 * The hop that was missing between "top users by cost" and the per-session
 * anatomy: start from the person, see what they were doing without reading
 * any of it. Every cell is a count, a token figure, a dollar figure, a date,
 * a model id or a flag — no titles, no summaries. The row's flags dot is the
 * top severity of the diagnoses that fired on the row's own numbers; the
 * full findings are on the session page.
 *
 * `costKnown=false` rows render "unknown", not "$0.00": 20% of session rows
 * fleet-wide have never had a cost aggregate written, and a zero there would
 * be a lie an admin acts on.
 *
 * Owned by the costs feature (scope `admin.costs`) even though it renders on
 * the user page (scope `admin.users`) — the host page decides whether the
 * viewer may see it; this component assumes it may.
 */
@Component({
  selector: 'app-user-conversations',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, DecimalPipe, SpinnerComponent],
  host: { class: 'block' },
  template: `
    <div class="p-6 bg-white border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-600">
      <div class="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 class="font-semibold">Conversations</h3>
          <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">
            Content-free: what each conversation cost and consumed, never what was said.
          </p>
        </div>
        <div class="flex items-center gap-2">
          <label class="sr-only" for="user-conversations-scope">Period</label>
          <select
            id="user-conversations-scope"
            [value]="allTime() ? 'all' : 'month'"
            (change)="onScopeChange($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="month">This month</option>
            <option value="all">All time</option>
          </select>
          <label class="sr-only" for="user-conversations-sort">Sort</label>
          <select
            id="user-conversations-sort"
            [value]="sort()"
            (change)="onSortChange($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="cost">Most expensive</option>
            <option value="recent">Most recent</option>
            <option value="context">Largest context</option>
            <option value="messages">Most messages</option>
          </select>
        </div>
      </div>

      @if (sessionsResource.isLoading()) {
        <div class="flex items-center gap-3 py-6 text-sm/6 text-gray-500 dark:text-gray-400">
          <app-spinner size="sm" label="Loading conversations" />
          Loading conversations…
        </div>
      } @else if (sessionsResource.error()) {
        <div
          role="alert"
          class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
        >
          Couldn't load this user's conversations.
          <button type="button" (click)="sessionsResource.reload()" class="ml-2 font-medium underline hover:no-underline">
            Retry
          </button>
        </div>
      } @else if (sessionsResource.hasValue()) {
        @if (sessionsResource.value().sessions.length === 0) {
          <p class="py-6 text-center text-sm/6 text-gray-500 dark:text-gray-400">
            {{ allTime() ? 'No conversations recorded for this user.' : 'No conversations active this month.' }}
          </p>
        } @else {
          <p class="mb-3 text-xs/5 text-gray-500 dark:text-gray-400" aria-live="polite">
            {{ summaryLine() }}
          </p>
          <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead>
                <tr class="text-left text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                  <th scope="col" class="py-2 pr-3">Conversation</th>
                  <th scope="col" class="px-3 py-2">Last active</th>
                  <th scope="col" class="px-3 py-2 text-right">Messages</th>
                  <th scope="col" class="px-3 py-2">Model</th>
                  <th scope="col" class="px-3 py-2 text-right">Tools on</th>
                  <th scope="col" class="px-3 py-2">Context</th>
                  <th scope="col" class="px-3 py-2 text-right">Cost</th>
                  <th scope="col" class="px-3 py-2 text-right">Waste</th>
                  <th scope="col" class="px-3 py-2">Flags</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                @for (s of sessionsResource.value().sessions; track s.sessionId) {
                  <tr
                    class="cursor-pointer text-sm/6 text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700/50"
                    (click)="open(s)"
                    (keydown.enter)="open(s)"
                    tabindex="0"
                    [attr.aria-label]="'Open session ' + s.sessionId"
                  >
                    <td class="py-2 pr-3">
                      <span class="font-mono text-xs/5 text-gray-900 dark:text-white" [title]="s.sessionId">{{ short(s.sessionId) }}</span>
                      @if (s.createdAt) {
                        <span class="ml-2 text-xs/5 text-gray-500 dark:text-gray-400">started {{ s.createdAt | date: 'MMM d' }}</span>
                      }
                      @if (s.agentBound) {
                        <span class="ml-2 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 text-gray-600 dark:bg-white/10 dark:text-gray-300">agent</span>
                      }
                      @if (s.status === 'deleted') {
                        <span
                          class="ml-2 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 text-gray-600 dark:bg-white/10 dark:text-gray-300"
                          title="Deleted by the user. Its cost rows and its share of the period total survive the delete."
                          >deleted</span
                        >
                      }
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 tabular-nums">
                      {{ s.lastMessageAt ? (s.lastMessageAt | date: 'MMM d, HH:mm') : '—' }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">{{ s.messageCount ?? '—' }}</td>
                    <td class="max-w-40 truncate px-3 py-2 font-mono text-xs/5" [title]="s.modelId ?? ''">
                      {{ modelShort(s.modelId) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">{{ s.enabledToolCount ?? '—' }}</td>
                    <td class="whitespace-nowrap px-3 py-2">
                      @if (s.lastContextTokens != null) {
                        <div class="flex items-center gap-2">
                          <div class="h-1.5 w-20 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700" aria-hidden="true">
                            <div class="h-full rounded-full" [class]="contextBarClass(s)" [style.width.%]="contextWidth(s)"></div>
                          </div>
                          <span class="tabular-nums text-xs/5" [title]="contextTitle(s)">{{ tokens(s.lastContextTokens) }}</span>
                        </div>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">—</span>
                      }
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      @if (s.costKnown && s.totalCost != null) {
                        <span class="font-medium text-gray-900 dark:text-white">{{ currency(s.totalCost) }}</span>
                        @if (s.shareOfUserPeriod != null) {
                          <span class="ml-1 text-xs/5 text-gray-500 dark:text-gray-400">{{ s.shareOfUserPeriod | number: '1.0-0' }}%</span>
                        }
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500" title="No cost aggregate was ever recorded for this conversation — it is unrecorded, not free">unknown</span>
                      }
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums" [class.text-state-danger-600]="(s.wastedUsd ?? 0) > 0" [class.dark:text-state-danger-400]="(s.wastedUsd ?? 0) > 0">
                      {{ (s.wastedUsd ?? 0) > 0 ? currency(s.wastedUsd!, 4) : '—' }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2">
                      @if (s.diagnosisCount > 0) {
                        <span class="inline-flex items-center gap-1.5 text-xs/5">
                          <span class="size-2 rounded-full" [class]="dot(s.topDiagnosisSeverity)" aria-hidden="true"></span>
                          {{ s.diagnosisCount }} {{ s.diagnosisCount === 1 ? 'finding' : 'findings' }}
                        </span>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">—</span>
                      }
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
      }
    </div>
  `,
})
export class UserConversationsComponent {
  private readonly costHttp = inject(AdminCostHttpService);
  private readonly router = inject(Router);

  readonly userId = input.required<string>();

  readonly allTime = signal(false);
  readonly sort = signal<UserSessionsSort>('cost');

  readonly sessionsResource = resource({
    params: () => ({ userId: this.userId(), allTime: this.allTime(), sort: this.sort() }),
    loader: ({ params }) =>
      firstValueFrom(
        this.costHttp.getUserSessions(params.userId, {
          allTime: params.allTime,
          sort: params.sort,
          limit: 100,
        }),
      ),
  });

  readonly summaryLine = computed(() => {
    if (!this.sessionsResource.hasValue()) return '';
    const v = this.sessionsResource.value();
    const shown = v.sessions.length;
    const parts = [
      shown < v.total ? `Showing ${shown} of ${v.total} conversations` : `${v.total} ${v.total === 1 ? 'conversation' : 'conversations'}`,
    ];
    if (v.userPeriodCost != null) parts.push(`${this.currency(v.userPeriodCost)} recorded this month`);
    if (v.unknownCostCount > 0) parts.push(`${v.unknownCostCount} with unrecorded cost`);
    if ((v.deletedSessionCount ?? 0) > 0) {
      // A delete is a tombstone, not a refund — say so, or the period total
      // will not add up to the rows on screen.
      parts.push(
        `${v.deletedSessionCount} deleted (${this.currency(v.deletedSessionCost ?? 0)} still counted in the total)`,
      );
    }
    return parts.join(' · ');
  });

  onScopeChange(event: Event): void {
    this.allTime.set((event.target as HTMLSelectElement).value === 'all');
  }

  onSortChange(event: Event): void {
    this.sort.set((event.target as HTMLSelectElement).value as UserSessionsSort);
  }

  open(s: UserSessionSummary): void {
    void this.router.navigate(['/admin/costs/sessions', s.sessionId]);
  }

  short(id: string): string {
    return shortSessionId(id);
  }

  tokens(n: number): string {
    return formatTokensShort(n);
  }

  dot(severity: UserSessionSummary['topDiagnosisSeverity']): string {
    return severityDotClass(severity);
  }

  modelShort(modelId: string | null | undefined): string {
    if (!modelId) return '—';
    // "us.anthropic.claude-haiku-4-5-20251001-v1:0" → "claude-haiku-4-5"
    const tail = modelId.split('.').pop() ?? modelId;
    return tail.replace(/-\d{8}.*$/, '').replace(/:.*$/, '');
  }

  contextWidth(s: UserSessionSummary): number {
    if (s.contextShare == null) return 0;
    return Math.min(100, Math.round(s.contextShare * 100));
  }

  contextBarClass(s: UserSessionSummary): string {
    const share = s.contextShare ?? 0;
    if (share >= 0.5) return 'bg-state-danger-500';
    if (share >= 0.25) return 'bg-state-warning-500';
    return 'bg-state-success-500';
  }

  contextTitle(s: UserSessionSummary): string {
    if (s.lastContextTokens == null) return '';
    const window = s.contextWindow ? ` of ${formatTokensShort(s.contextWindow)} window` : '';
    return `${new Intl.NumberFormat('en-US').format(s.lastContextTokens)} tokens in context at the last call${window}`;
  }

  currency(value: number, maxFractionDigits = 2): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: maxFractionDigits,
    }).format(value);
  }
}
