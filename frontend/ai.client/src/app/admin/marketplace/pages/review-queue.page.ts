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
import { reachabilityReviewerMessage } from '../../../agents/models/reachability';
import {
  RequestChangesDialogComponent,
  RequestChangesDialogData,
  RequestChangesDialogResult,
} from '../components/request-changes-dialog.component';
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
  imports: [NgIcon, AgentTileComponent],
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
                class="flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-center dark:border-gray-700 dark:bg-gray-800"
              >
                <app-agent-tile [agentId]="row.agentId" [iconUrl]="row.iconUrl" [emoji]="row.emoji" />

                <div class="min-w-0 flex-1">
                  <h2 class="truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
                    {{ row.name }}
                  </h2>
                  <p class="truncate text-sm/6 text-gray-500 dark:text-gray-400">
                    {{ row.ownerName }} · {{ row.category }} · submitted
                    {{ relativeTime(row.submittedAt) }}
                  </p>
                  @if (row.tagline) {
                    <p class="truncate text-sm/6 text-gray-500 dark:text-gray-400">
                      {{ row.tagline }}
                    </p>
                  }

                  <!-- Reachability. Never collapsed away and never a blocker: approving a
                       PRIVATE agent shelves a tile that 404s for everyone but its author,
                       and that is the one fact the reviewer cannot get from anywhere else
                       on this row. -->
                  @if (reachabilityWarning(row); as warning) {
                    <p
                      class="mt-1 flex items-start gap-1.5 text-sm/6 text-amber-700 dark:text-amber-400"
                    >
                      <ng-icon
                        name="heroEyeSlash"
                        class="mt-1 size-4 shrink-0"
                        aria-hidden="true"
                      />
                      <span>{{ warning }}</span>
                    </p>
                  }
                </div>

                <div class="flex shrink-0 gap-2">
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
                </div>
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

  async approve(row: AdminListingRow): Promise<void> {
    await this.decide(row, { decision: 'approve' });
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
