import { Component, ChangeDetectionStrategy, inject, signal, OnInit } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheck,
  heroExclamationTriangle,
  heroFlag,
  heroNoSymbol,
} from '@ng-icons/heroicons/outline';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AdminReportRow, ReportReason } from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import {
  ResolveReportDialogComponent,
  ResolveReportDialogData,
  ResolveReportDialogResult,
} from '../components/resolve-report-dialog.component';

/**
 * The Reports queue — user-submitted problem reports (D10, D15).
 *
 * The second work stream beside submissions, and deliberately shaped like it: a card
 * list where each row is one decision. Three things about this surface are load-bearing.
 *
 * **The reporter is shown here and nowhere else (D15.2).** Admins need identity to spot a
 * brigade or a grudge — three reports on one agent from one person reads very differently
 * from three from three. The author never sees it.
 *
 * **Closing a report is not a takedown (D15.5).** Resolve and Dismiss write only the
 * report. The row links to the agent so the reviewer can act on it properly if it
 * warrants that, and the dialog says so explicitly — an admin who thinks "Resolve" delists
 * will believe a problem has been handled when only the queue has been tidied.
 *
 * **Severity leads the sweep.** Rows arrive ordered `(severity, oldest-first)` from the
 * server, so `inappropriate` sits at the top rather than waiting its turn behind a
 * stale-link complaint. The page renders that order; it does not re-sort.
 */
@Component({
  selector: 'app-marketplace-reports',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, AgentTileComponent, TooltipDirective],
  providers: [provideIcons({ heroCheck, heroExclamationTriangle, heroFlag, heroNoSymbol })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Reports</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Problems users reported from an agent's page. These are private — they never
            appear in the store, and the author is never told who reported them. Resolving one
            records your decision; it does not change the agent.
          </p>
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ error() }}
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading reports</span>
          </div>
        } @else if (reports().length === 0) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroFlag"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              Nothing reported
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Problems users report from an agent's page appear here.
            </p>
          </div>
        } @else {
          <ul class="flex flex-col gap-3">
            @for (row of reports(); track row.reportId) {
              <li
                class="rounded-2xl border bg-white p-4 dark:bg-gray-800"
                [class]="
                  row.reason === 'inappropriate'
                    ? 'border-rose-300 dark:border-rose-800'
                    : 'border-gray-200 dark:border-gray-700'
                "
              >
                <div class="flex flex-col gap-3 sm:flex-row sm:items-start">
                  <app-agent-tile
                    [agentId]="row.agentId"
                    [iconUrl]="row.iconUrl"
                    [emoji]="row.emoji"
                  />

                  <div class="min-w-0 flex-1">
                    <div class="flex flex-wrap items-center gap-2">
                      <h2 class="truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
                        {{ row.agentName }}
                      </h2>
                      <span
                        class="inline-flex items-center rounded-full px-2 py-0.5 text-xs/5 font-semibold"
                        [class]="reasonClass(row.reason)"
                      >
                        {{ reasonLabel(row.reason) }}
                      </span>
                      @if (row.agentMissing) {
                        <span
                          class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs/5 font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                          [appTooltip]="
                            'This agent has been deleted. Reports are deleted with their agent, so this row was orphaned by an older delete — dismiss it to clear it.'
                          "
                          appTooltipPosition="top"
                        >
                          <ng-icon name="heroExclamationTriangle" class="size-3.5" aria-hidden="true" />
                          Agent deleted
                        </span>
                      } @else if (row.listingState && row.listingState !== 'published') {
                        <span
                          class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-xs/5 font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                          [appTooltip]="'Someone has already changed this listing'"
                          appTooltipPosition="top"
                        >
                          {{ listingLabel(row.listingState) }}
                        </span>
                      }
                    </div>

                    <!--
                      D15.2 — the reporter, admin-only. Shown next to the timestamp
                      because the pattern an admin is looking for is "same person, again".
                    -->
                    <p class="mt-0.5 truncate text-sm/6 text-gray-500 dark:text-gray-400">
                      Reported by {{ row.reporterName }} · {{ relativeTime(row.createdAt) }}
                      @if (row.ownerName) {
                        · author {{ row.ownerName }}
                      }
                    </p>

                    @if (row.note) {
                      <p
                        class="mt-2 whitespace-pre-wrap rounded-2xl bg-gray-50 px-3 py-2 text-sm/6 text-gray-700 dark:bg-gray-900 dark:text-gray-300"
                      >
                        {{ row.note }}
                      </p>
                    }
                  </div>

                  <div class="flex shrink-0 flex-wrap gap-2">
                    @if (!row.agentMissing) {
                      <a
                        [routerLink]="['/agents', row.agentId]"
                        [appTooltip]="'Open this agent as a user sees it'"
                        appTooltipPosition="top"
                        class="inline-flex items-center rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                      >
                        View agent
                      </a>
                    }
                    <button
                      type="button"
                      [disabled]="busyId() === row.reportId"
                      (click)="triage(row, 'dismiss')"
                      [appTooltip]="'No action needed — takes it off the queue'"
                      appTooltipPosition="top"
                      class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      <ng-icon name="heroNoSymbol" class="size-4" aria-hidden="true" />
                      Dismiss
                    </button>
                    <button
                      type="button"
                      [disabled]="busyId() === row.reportId"
                      (click)="triage(row, 'resolve')"
                      [appTooltip]="'Handled — records your decision, does not change the agent'"
                      appTooltipPosition="top"
                      class="inline-flex items-center gap-1.5 rounded-2xl bg-blue-600 px-3 py-1.5 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                    >
                      <ng-icon name="heroCheck" class="size-4" aria-hidden="true" />
                      Resolve
                    </button>
                  </div>
                </div>
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class ReportsPage implements OnInit {
  private service = inject(AdminMarketplaceService);
  private dialog = inject(Dialog);

  readonly reports = signal<AdminReportRow[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly busyId = signal<string | null>(null);

  ngOnInit(): void {
    void this.reload();
  }

  async reload(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.reports.set(await this.service.loadReports());
    } catch {
      this.error.set(this.service.error() ?? 'Failed to load reports.');
    } finally {
      this.loading.set(false);
    }
  }

  async triage(row: AdminReportRow, decision: 'resolve' | 'dismiss'): Promise<void> {
    const ref = this.dialog.open<ResolveReportDialogResult, ResolveReportDialogData>(
      ResolveReportDialogComponent,
      { data: { report: row, decision } },
    );
    const result = await firstValueFrom(ref.closed);
    if (!result) return;

    this.busyId.set(row.reportId);
    this.error.set(null);
    try {
      await this.service.resolveReport(row.agentId, row.reportId, { decision, note: result.note });
      // Reload rather than splice: the decision clears the row's index key server-side, and
      // a reload is the only way the nav badge count stays honest.
      await this.reload();
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(typeof detail === 'string' ? detail : 'Failed to record the decision.');
    } finally {
      this.busyId.set(null);
    }
  }

  /** What the reporter picked, in the reviewer's words. */
  reasonLabel(reason: ReportReason): string {
    switch (reason) {
      case 'inaccurate':
        return 'Wrong answers';
      case 'broken':
        return 'Not working';
      case 'inappropriate':
        return 'Inappropriate';
      default:
        return 'Other';
    }
  }

  /** Only `inappropriate` gets colour — everything else is a queue item, not an alarm. */
  reasonClass(reason: ReportReason): string {
    return reason === 'inappropriate'
      ? 'bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300'
      : 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300';
  }

  listingLabel(state: string): string {
    switch (state) {
      case 'taken_down':
        return 'Taken down';
      case 'in_review':
        return 'In review';
      case 'changes_requested':
        return 'Changes requested';
      case 'private':
        return 'Unpublished';
      default:
        return state;
    }
  }

  /** Matches the Review queue's subtitle wording, so the two streams read alike. */
  relativeTime(iso?: string): string {
    if (!iso) return 'recently';
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return 'recently';

    const days = Math.floor((Date.now() - then) / 86_400_000);
    if (days <= 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 7) return `${days} days ago`;
    if (days < 14) return 'last week';
    if (days < 60) return `${Math.floor(days / 7)} weeks ago`;
    return `${Math.floor(days / 30)} months ago`;
  }
}
