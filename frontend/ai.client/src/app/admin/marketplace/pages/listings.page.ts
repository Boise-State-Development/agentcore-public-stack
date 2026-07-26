import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroRectangleStack,
  heroCheckBadge,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import {
  AdminListingRow,
  LISTING_DRIFT_CLASSES,
  LISTING_DRIFT_LABELS,
  LISTING_DRIFT_TOOLTIPS,
  LISTING_STATE_CLASSES,
  LISTING_STATE_LABELS,
  ListingState,
} from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';
import {
  TakedownDialogComponent,
  TakedownDialogData,
  TakedownDialogResult,
} from '../components/takedown-dialog.component';

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
  imports: [NgIcon, AgentTileComponent, TooltipDirective],
  providers: [
    provideIcons({ heroRectangleStack, heroCheckBadge, heroExclamationTriangle }),
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
              class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
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
            <span class="sr-only">Loading listings</span>
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
                              class="size-4 text-blue-600 dark:text-blue-400"
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
                          Post-approval drift (#744). Sits under the state badge rather than
                          in its own column: it only ever applies to published rows, so a
                          column would be mostly empty and would push the table wider.
                        -->
                        @if (row.drift; as drift) {
                          <span
                            class="inline-flex items-center gap-1 rounded-lg px-2 py-0.5 text-xs font-medium"
                            [class]="driftClasses[drift]"
                            [appTooltip]="driftTooltips[drift]"
                            appTooltipPosition="top"
                          >
                            <!--
                              The icon is only on the measured signal. The inferred one is
                              a maybe, and a warning triangle would overstate it.
                            -->
                            @if (drift === 'instructions') {
                              <ng-icon
                                name="heroExclamationTriangle"
                                class="size-3.5"
                                aria-hidden="true"
                              />
                            }
                            {{ driftLabels[drift] }}
                          </span>
                        }
                      </div>
                    </td>
                    <td class="px-4 py-3 text-right tabular-nums text-gray-600 dark:text-gray-300">
                      {{ row.usageCount.toLocaleString() }}
                    </td>
                    <td class="px-4 py-3 text-right">
                      @if (row.state === 'published') {
                        <button
                          type="button"
                          [disabled]="busyId() === row.agentId"
                          (click)="takedown(row)"
                          class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-rose-700 hover:bg-rose-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-rose-400 dark:hover:bg-gray-700"
                        >
                          Take down
                        </button>
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
  readonly driftLabels = LISTING_DRIFT_LABELS;
  readonly driftClasses = LISTING_DRIFT_CLASSES;
  readonly driftTooltips = LISTING_DRIFT_TOOLTIPS;

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
