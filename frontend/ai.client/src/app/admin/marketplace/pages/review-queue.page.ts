import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  OnInit,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroInbox, heroCheck, heroArrowUturnLeft, heroEyeSlash } from '@ng-icons/heroicons/outline';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AdminListingRow } from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';
import { ReviewDiffComponent } from '../components/review-diff.component';
import { reachabilityReviewerMessage } from '../../../agents/models/reachability';
import {
  RequestChangesDialogComponent,
  RequestChangesDialogData,
  RequestChangesDialogResult,
} from '../components/request-changes-dialog.component';
import {
  WithdrawalDecisionDialogComponent,
  WithdrawalDecisionDialogData,
  WithdrawalDecisionDialogResult,
} from '../components/withdrawal-decision-dialog.component';
import { parseIso } from '../../../utils/date';

/**
 * The Review queue — every submission awaiting a decision (D2).
 *
 * A card list rather than a table, matching the design mockup: each row is one decision,
 * and the subtitle carries the three things a reviewer needs before making it (who wrote
 * it, what shelf it wants, how long it has waited).
 *
 * The mockup's "Preview" action is deliberately absent: it opens the agent detail page,
 * which is Phase 3. Rendering a dead button would be worse than not having one.
 */
@Component({
  selector: 'app-review-queue',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentTileComponent, ReviewDiffComponent],
  providers: [provideIcons({ heroInbox, heroCheck, heroArrowUturnLeft, heroEyeSlash })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Review queue</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Every submission lands here. Approving publishes it to the store immediately;
            requesting changes returns it to the author with your note attached to their card.
          </p>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Authors asking to pull a live listing land here too. Those stay published until
            you decide.
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
            <span class="sr-only">Loading submissions</span>
          </div>
        } @else if (submissions().length === 0) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroInbox"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              Nothing awaiting review
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Submissions from agent authors appear here.
            </p>
          </div>
        } @else {
          <ul class="flex flex-col gap-3">
            @for (row of submissions(); track row.agentId) {
              <li
                class="flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800"
              >
                <!-- The decision row. The reachability warning is deliberately NOT in
                     here — see the note on it below. -->
                <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
                <app-agent-tile [agentId]="row.agentId" [iconUrl]="row.iconUrl" [emoji]="row.emoji" />

                <div class="min-w-0 flex-1">
                  <h2 class="flex items-center gap-2 text-sm/6 font-semibold text-gray-900 dark:text-white">
                    <span class="truncate">{{ row.name }}</span>
                    <!-- The two things in this queue want opposite answers, and without a
                         label they render identically. A withdrawal request said "take this
                         down"; answering it with the submission verbs re-publishes over the
                         author's request without ever saying so. -->
                    @if (isWithdrawal(row)) {
                      <span
                        class="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-xs/5 font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
                        >Withdrawal requested</span
                      >
                    }
                  </h2>
                  <p class="truncate text-sm/6 text-gray-500 dark:text-gray-400">
                    @if (isWithdrawal(row)) {
                      {{ row.ownerName }} · {{ row.category }} · asked to pull it
                      {{ relativeTime(row.withdrawalRequestedAt) }}
                    } @else {
                      {{ row.ownerName }} · {{ row.category }} · submitted
                      {{ relativeTime(row.submittedAt) }}
                    }
                  </p>
                  @if (row.tagline) {
                    <p class="truncate text-sm/6 text-gray-500 dark:text-gray-400">
                      {{ row.tagline }}
                    </p>
                  }
                  @if (isWithdrawal(row)) {
                    <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                      Still live in the store while you decide — a request is not a removal.
                    </p>
                  }

                  <!-- §6.1 — what this submission changes against what is published.
                       Collapsed by default and fetched on expand: the queue is a list of
                       decisions, and pre-loading a diff per row would pull every pending
                       agent's full instructions down to render a control nobody opened.

                       Not shown on a withdrawal request: nothing changed, and the pending
                       version *is* the published one, so it would offer a reviewer a
                       guaranteed-empty diff next to a decision about taking it down. -->
                  @if (!isWithdrawal(row)) {
                    <app-review-diff [agentId]="row.agentId" />
                  }
                </div>

                <div class="flex shrink-0 gap-2">
                  @if (isWithdrawal(row)) {
                    <button
                      type="button"
                      [disabled]="busyId() === row.agentId"
                      (click)="decideWithdrawal(row, 'decline')"
                      class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      <ng-icon name="heroArrowUturnLeft" class="size-4" aria-hidden="true" />
                      Keep published
                    </button>
                    <button
                      type="button"
                      [disabled]="busyId() === row.agentId"
                      (click)="decideWithdrawal(row, 'grant')"
                      class="inline-flex items-center gap-1.5 rounded-2xl bg-rose-600 px-3 py-1.5 text-sm/6 font-medium text-white hover:bg-rose-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <ng-icon name="heroEyeSlash" class="size-4" aria-hidden="true" />
                      Take it down
                    </button>
                  } @else {
                    <button
                      type="button"
                      [disabled]="busyId() === row.agentId"
                      (click)="requestChanges(row)"
                      class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      <ng-icon name="heroArrowUturnLeft" class="size-4" aria-hidden="true" />
                      Request changes
                    </button>
                    <button
                      type="button"
                      [disabled]="busyId() === row.agentId"
                      (click)="approve(row)"
                      class="inline-flex items-center gap-1.5 rounded-2xl bg-blue-600 px-3 py-1.5 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                    >
                      <ng-icon name="heroCheck" class="size-4" aria-hidden="true" />
                      Approve
                    </button>
                  }
                </div>
                </div>

                <!-- Reachability, on its own full-width row.
                     It started inside the identity column, where it was squeezed to ~160px
                     of a 528px card and wrapped across five lines. A sentence the reviewer
                     has to work to read is one they will skip, which defeats the point of
                     surfacing it at all. Never collapsed away and never a blocker:
                     approving a PRIVATE agent shelves a tile that 404s for everyone but
                     its author, and that is the one fact this row cannot otherwise tell
                     them. -->
                @if (reachabilityWarning(row); as warning) {
                  <p
                    class="flex items-start gap-1.5 border-t border-gray-100 pt-3 text-sm/6 text-amber-700 dark:border-gray-700 dark:text-amber-400"
                  >
                    <ng-icon name="heroEyeSlash" class="mt-1 size-4 shrink-0" aria-hidden="true" />
                    <span>{{ warning }}</span>
                  </p>
                }
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class ReviewQueuePage implements OnInit {
  private service = inject(AdminMarketplaceService);
  private dialog = inject(Dialog);

  readonly submissions = signal<AdminListingRow[]>([]);
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
      this.submissions.set(await this.service.loadSubmissions());
    } catch {
      this.error.set(this.service.error() ?? 'Failed to load submissions.');
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Whether this row is an author asking to pull a live listing rather than a submission.
   *
   * Keyed on `state`, not on the timestamp: the timestamp is what the row *renders*, and a
   * row that lost it would silently fall back to the submission verbs — which is the exact
   * failure this predicate exists to prevent.
   */
  isWithdrawal(row: AdminListingRow): boolean {
    return row.state === 'withdrawal_requested';
  }

  async approve(row: AdminListingRow): Promise<void> {
    await this.decide(row, { decision: 'approve' });
  }

  /**
   * Grant or decline a withdrawal request.
   *
   * A separate endpoint from `review`, deliberately — see `decideWithdrawal` on the
   * service. Routing this through `approve` is what the queue used to do by omission, and
   * it silently re-published listings whose authors had asked for them to come down.
   */
  async decideWithdrawal(row: AdminListingRow, decision: 'grant' | 'decline'): Promise<void> {
    const ref = this.dialog.open<
      WithdrawalDecisionDialogResult,
      WithdrawalDecisionDialogData
    >(WithdrawalDecisionDialogComponent, { data: { listing: row, decision } });
    // `undefined` is cancel; `''` is a deliberate note-less grant, so compare explicitly.
    const note = await firstValueFrom(ref.closed);
    if (note === undefined) return;

    this.busyId.set(row.agentId);
    this.error.set(null);
    try {
      await this.service.decideWithdrawal(row.agentId, { decision, note: note || undefined });
      await this.reload();
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(
        typeof detail === 'string' ? detail : 'Failed to record the withdrawal decision.',
      );
    } finally {
      this.busyId.set(null);
    }
  }

  async requestChanges(row: AdminListingRow): Promise<void> {
    const ref = this.dialog.open<RequestChangesDialogResult, RequestChangesDialogData>(
      RequestChangesDialogComponent,
      { data: { listing: row } },
    );
    const note = await firstValueFrom(ref.closed);
    if (note) {
      await this.decide(row, { decision: 'request_changes', note });
    }
  }

  private async decide(
    row: AdminListingRow,
    request: { decision: 'approve' | 'request_changes'; note?: string },
  ): Promise<void> {
    this.busyId.set(row.agentId);
    this.error.set(null);
    try {
      await this.service.review(row.agentId, request);
      // Reload rather than splice: approving writes the directory key server-side, and a
      // reload is the only way the pending count stays honest.
      await this.reload();
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(typeof detail === 'string' ? detail : 'Failed to record the decision.');
    } finally {
      this.busyId.set(null);
    }
  }

  /** "yesterday" / "3 days ago", matching the mockup's queue subtitle. */
  /**
   * The reviewer-facing half of the shared wording. Kept as a one-line delegation so the
   * author's dialog and this queue can never describe the same state differently.
   */
  reachabilityWarning(row: AdminListingRow): string | null {
    return reachabilityReviewerMessage(row.reachability);
  }

  relativeTime(iso?: string): string {
    if (!iso) return 'recently';
    const then = parseIso(iso).getTime();
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
