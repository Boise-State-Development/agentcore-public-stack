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
import { heroRectangleStack, heroCheckBadge } from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import {
  AdminListingRow,
  PUBLISHED_VERSION_CLASSES,
  PUBLISHED_VERSION_TOOLTIP,
  LISTING_STATE_CLASSES,
  LISTING_STATE_LABELS,
  ListingState,
} from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import {
  TakedownDialogComponent,
  TakedownDialogData,
  TakedownDialogResult,
} from '../components/takedown-dialog.component';
import {
  RequestChangesDialogComponent,
  RequestChangesDialogData,
  RequestChangesDialogResult,
} from '../components/request-changes-dialog.component';
import {
  RollbackDialogComponent,
  RollbackDialogData,
  RollbackDialogResult,
} from '../components/rollback-dialog.component';

/**
 * Listings — every agent that has ever been submitted (D10).
 *
 * The mockup's table lists only published agents; this one carries a state filter and a
 * state badge because until the Discover page ships (Phase 2) this is the *only* view of
 * the marketplace, and an admin needs to see the ones that are not live too.
 *
 * The mockup's inline category `<select>` is deliberately absent; category is shown as
 * text rather than rendered as a control that cannot be honored here.
 *
 * Promotion to the store front is **not** a star on this table (Phase 5). The featured row
 * is an ordered list, and a per-row toggle can express membership but not position — an
 * admin promoting three agents from here would have no way to say which comes first. It
 * lives on the Store Front surface, where the order is the thing being edited.
 */
@Component({
  selector: 'app-marketplace-listings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentTileComponent, TooltipDirective, SpinnerComponent],
  providers: [
    provideIcons({ heroRectangleStack, heroCheckBadge }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Listings</h1>
          <p class="mt-1 max-w-3xl text-sm/6 text-gray-600 dark:text-gray-400">
            Every agent that has been submitted to the store. Taking one down removes it from
            the store but leaves it reachable by direct link for anyone mid-conversation.
          </p>
        </div>

        <div class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <label for="state" class="sr-only">Filter by state</label>
            <select
              id="state"
              [value]="stateFilter()"
              (change)="onStateFilterChange($event)"
              class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
            >
              <option value="">All states</option>
              @for (state of states; track state) {
                <option [value]="state">{{ stateLabels[state] }}</option>
              }
            </select>
          </div>
          <p class="text-sm/6 text-gray-500 dark:text-gray-400">
            {{ listings().length }} {{ listings().length === 1 ? 'listing' : 'listings' }}
          </p>
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ error() }}
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <app-spinner size="lg" label="Loading listings" />
          </div>
        } @else if (listings().length === 0) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroRectangleStack"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              No listings yet
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Agents appear here once their authors submit them for review.
            </p>
          </div>
        } @else {
          <div
            class="overflow-x-auto rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800"
          >
            <table class="min-w-full text-sm/6">
              <thead>
                <tr class="border-b border-gray-200 dark:border-gray-700">
                  <th scope="col" class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Agent</th>
                  <th scope="col" class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Publisher</th>
                  <th scope="col" class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Category</th>
                  <th scope="col" class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">State</th>
                  <th scope="col" class="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Chats</th>
                  <th scope="col" class="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                    <span class="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                @for (row of listings(); track row.agentId) {
                  <tr class="border-t border-gray-200 dark:border-gray-700">
                    <td class="px-4 py-3">
                      <div class="flex items-center gap-3">
                        <app-agent-tile
                          [agentId]="row.agentId"
                          [iconUrl]="row.iconUrl"
                          [emoji]="row.emoji"
                          size="sm"
                        />
                        <div class="min-w-0">
                          <p class="truncate font-medium text-gray-900 dark:text-white">
                            {{ row.name }}
                          </p>
                          <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                            by {{ row.ownerName }}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td class="px-4 py-3 text-gray-600 dark:text-gray-300">
                      @if (row.publisher) {
                        <span class="inline-flex items-center gap-1">
                          {{ row.publisher.label }}
                          @if (row.publisher.verified) {
                            <ng-icon
                              name="heroCheckBadge"
                              class="size-4 text-state-info-600 dark:text-state-info-400"
                              [appTooltip]="'Verified publisher'"
                              appTooltipPosition="top"
                            />
                          }
                        </span>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">Unattributed</span>
                      }
                    </td>
                    <td class="px-4 py-3 text-gray-600 dark:text-gray-300">{{ row.category }}</td>
                    <td class="px-4 py-3">
                      <div class="flex flex-col items-start gap-1">
                        <span
                          class="inline-flex rounded-lg px-2 py-0.5 text-xs font-semibold"
                          [class]="stateClasses[row.state]"
                        >
                          {{ stateLabels[row.state] }}
                        </span>
                        <!--
                          Which snapshot is live. Sits under the state badge rather than in
                          its own column: it only ever applies to published rows, so a
                          column would be mostly empty and would push the table wider.
                          (This replaced the post-approval drift marker — see the model.)
                        -->
                        @if (row.publishedVersion; as version) {
                          <span
                            class="inline-flex items-center gap-1 rounded-lg px-2 py-0.5 text-xs font-medium tabular-nums"
                            [class]="publishedVersionClasses"
                            [appTooltip]="publishedVersionTooltip"
                            appTooltipPosition="top"
                          >
                            v{{ version }}
                          </span>
                        }
                      </div>
                    </td>
                    <td class="px-4 py-3 text-right tabular-nums text-gray-600 dark:text-gray-300">
                      {{ row.usageCount.toLocaleString() }}
                    </td>
                    <td class="px-4 py-3 text-right">
                      @if (row.state === 'published') {
                        <div class="flex justify-end gap-2">
                          <!-- The gentler half of the pair, and the only route to a
                               resubmission that still has a published version to diff
                               against: every other way back (take down, granted withdrawal)
                               clears the published-version pointer first, so the review
                               diff has nothing to compare against without this. -->
                          <button
                            type="button"
                            [disabled]="busyId() === row.agentId"
                            (click)="requestChanges(row)"
                            class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                          >
                            Request changes
                          </button>
                          <!-- Only once there is a second version to switch to. A control that
                               opens a dialog whose only honest content is "there is nothing
                               here" is worse than an absent one.

                               Labelled for the operation, not one direction of it: after a
                               rollback the same control is how an admin puts the newer
                               version back, and "Roll back" would name the opposite of what
                               it then does. -->
                          @if (canRollBack(row)) {
                            <button
                              type="button"
                              [disabled]="busyId() === row.agentId"
                              (click)="rollback(row)"
                              class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                            >
                              Change version
                            </button>
                          }
                          <button
                            type="button"
                            [disabled]="busyId() === row.agentId"
                            (click)="takedown(row)"
                            class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-state-danger-400 dark:hover:bg-gray-700"
                          >
                            Take down
                          </button>
                        </div>
                      }
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
      </div>
    </div>
  `,
})
export class MarketplaceListingsPage implements OnInit {
  private service = inject(AdminMarketplaceService);
  private dialog = inject(Dialog);

  readonly listings = signal<AdminListingRow[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly busyId = signal<string | null>(null);
  readonly stateFilter = signal<'' | ListingState>('');

  readonly states: ListingState[] = [
    'in_review',
    'published',
    'changes_requested',
    'taken_down',
    'private',
  ];
  readonly stateLabels = LISTING_STATE_LABELS;
  readonly stateClasses = LISTING_STATE_CLASSES;
  readonly publishedVersionClasses = PUBLISHED_VERSION_CLASSES;
  readonly publishedVersionTooltip = PUBLISHED_VERSION_TOOLTIP;

  ngOnInit(): void {
    void this.reload();
  }

  onStateFilterChange(event: Event): void {
    this.stateFilter.set((event.target as HTMLSelectElement).value as '' | ListingState);
    void this.reload();
  }

  async reload(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const filter = this.stateFilter();
      this.listings.set(await this.service.loadListings(filter || undefined));
    } catch {
      this.error.set(this.service.error() ?? 'Failed to load listings.');
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Send a *live* listing back to its author with a note, without taking it down.
   *
   * The listing keeps serving its approved version while the author revises — this is
   * feedback, not a delisting, and `takedown` is the button for the other intent.
   *
   * It is also the only transition that leaves `publishedVersion` intact, which makes it
   * the only way a resubmission ever arrives with something to diff against. Without this
   * control the review diff renders "first submission" on every submission an agent ever
   * makes, however many versions it has.
   */
  async requestChanges(row: AdminListingRow): Promise<void> {
    const ref = this.dialog.open<RequestChangesDialogResult, RequestChangesDialogData>(
      RequestChangesDialogComponent,
      { data: { listing: row } },
    );
    const note = await firstValueFrom(ref.closed);
    if (!note) return;

    this.busyId.set(row.agentId);
    this.error.set(null);
    try {
      await this.service.review(row.agentId, { decision: 'request_changes', note });
      await this.reload();
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(typeof detail === 'string' ? detail : 'Failed to record the decision.');
    } finally {
      this.busyId.set(null);
    }
  }

  /**
   * Whether another snapshot exists to switch to.
   *
   * ⚠️ Asked as "does a second version exist?", not "are we serving above `v1`?". Those read
   * the same until someone rolls back, and then they diverge in the one state where the
   * control matters most: a listing rolled back to `v1` still has every later version
   * sitting there, and repointing at one is the *same operation* in the other direction —
   * which is exactly what the dialog promises ("the current version stays available to roll
   * forward to"). Gating on `publishedVersion > 1` hid the button precisely then, stranding
   * the rollback with no way to undo it from the UI even though the endpoint accepts it.
   *
   * `latestVersion` is the high-water mark and survives the pointer moving down, so it
   * answers the question the affordance is really asking. Still inferred rather than fetched
   * — a page of rows must not fetch every agent's history — and the dialog loads the real
   * list and says so honestly in the edge the inference cannot see.
   */
  canRollBack(row: AdminListingRow): boolean {
    return (row.latestVersion ?? 0) > 1;
  }

  /**
   * Repoint a published listing at an earlier snapshot (§8).
   *
   * Not a review decision — nothing is cut, nothing queues, and the listing stays published.
   * It changes only which approved artifact the store serves, which is why it sits beside
   * "Take down" rather than in the review queue.
   */
  async rollback(row: AdminListingRow): Promise<void> {
    const ref = this.dialog.open<RollbackDialogResult, RollbackDialogData>(
      RollbackDialogComponent,
      { data: { listing: row } },
    );
    const choice = await firstValueFrom(ref.closed);
    if (!choice) return;

    this.busyId.set(row.agentId);
    this.error.set(null);
    try {
      await this.service.rollback(row.agentId, choice);
      await this.reload();
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(typeof detail === 'string' ? detail : 'Failed to roll back the listing.');
    } finally {
      this.busyId.set(null);
    }
  }

  async takedown(row: AdminListingRow): Promise<void> {
    const ref = this.dialog.open<TakedownDialogResult, TakedownDialogData>(
      TakedownDialogComponent,
      { data: { listing: row } },
    );
    const reason = await firstValueFrom(ref.closed);
    if (!reason) return;

    this.busyId.set(row.agentId);
    this.error.set(null);
    try {
      await this.service.takedown(row.agentId, reason);
      await this.reload();
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(typeof detail === 'string' ? detail : 'Failed to take down the listing.');
    } finally {
      this.busyId.set(null);
    }
  }
}
