import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroMagnifyingGlass, heroSparkles } from '@ng-icons/heroicons/outline';
import { RouterLink } from '@angular/router';
import { AgentStoreService } from '../services/agent-store.service';
import { AgentPinService } from '../services/agent-pin.service';
import { AgentListing, CategoryShelf } from '../models/store.model';
import { AgentsTabsComponent } from '../components/agents-tabs.component';
import { AgentIconComponent } from '../components/agent-icon.component';
import { AgentListingRowComponent } from '../components/agent-listing-row.component';

/**
 * Discover — the browse surface over published Agents (D4, D10, Phases 2 and 5).
 *
 * Rows carry an icon, a name and one line, and nothing else: no model chip, no tool or
 * skill counts, no chat counts, no runnability badge. Those numbers are still collected
 * and still surfaced — on the detail page and in admin reporting — but a store row that
 * reports its own dependency list is a spec sheet, and it scans like one.
 *
 * Above the shelves sit two rows that are not shelves. The **Pinned strip** is the
 * user's own set, here because arriving at a store you have used before and seeing none
 * of your own choices is disorienting. The **Featured row** is the admin's curation, and
 * it is the store's *only* ranking lever — everything below it is newest-first, because
 * `GSI5_SK` is `created_at` and v1 ships no popularity sort. Both are hidden when empty.
 *
 * Search filters what has been loaded rather than issuing a query per keystroke. The
 * store is small enough that this is honest; a real full-corpus search arrives with the
 * Registry catalog, and pretending to have one now would mean silently missing results
 * past the first page.
 */
/** How many pins the strip shows before deferring to the Pinned tab. */
const PINNED_STRIP_LIMIT = 8;

@Component({
  selector: 'app-agent-discover',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    RouterLink,
    AgentsTabsComponent,
    AgentIconComponent,
    AgentListingRowComponent,
  ],
  providers: [provideIcons({ heroMagnifyingGlass, heroSparkles })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <app-agents-tabs />

        <div class="mt-6 mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Discover agents</h1>
          <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
            Agents published by teams across the university.
          </p>
        </div>

        <div class="relative mb-8 max-w-md">
          <ng-icon
            name="heroMagnifyingGlass"
            class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
            aria-hidden="true"
          />
          <label for="agent-search" class="sr-only">Search agents</label>
          <input
            type="search"
            id="agent-search"
            [value]="query()"
            (input)="onSearch($event)"
            placeholder="Search agents…"
            class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ error() }}
          </div>
        }

        <!-- Separate from the store's own error: a row's add control failing is a
             different event from the shelves failing to load, and a pin that silently
             does nothing is the worst of the three outcomes. -->
        @if (pinError()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ pinError() }}
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading agents</span>
          </div>
        } @else if (isEmpty()) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroSparkles"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              {{ query() ? 'No agents match your search' : 'No published agents yet' }}
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              {{
                query()
                  ? 'Try a different word, or clear the search to browse everything.'
                  : 'Agents appear here once their authors submit them and an admin approves.'
              }}
            </p>
          </div>
        } @else if (query()) {
          <!-- Search collapses the category sections into one flat result list. -->
          <section>
            <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
              {{ searchResults().length }}
              {{ searchResults().length === 1 ? 'result' : 'results' }}
            </h2>
            <ul class="grid gap-x-8 sm:grid-cols-2">
              @for (listing of searchResults(); track listing.agentId) {
                <li>
                  <app-agent-listing-row [listing]="listing" />
                </li>
              }
            </ul>
          </section>
        } @else {
          @if (pinned().length) {
            <section class="mb-8">
              <div class="mb-3 flex items-baseline justify-between gap-4">
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                  Your pinned agents
                </h2>
                <a
                  routerLink="/agents/pinned"
                  class="text-sm/6 font-medium text-blue-600 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-blue-400"
                >
                  See all
                </a>
              </div>
              <ul class="flex flex-wrap gap-2">
                @for (pin of pinnedStrip(); track pin.agentId) {
                  <li>
                    <a
                      [routerLink]="['/agents', pin.agentId]"
                      class="flex items-center gap-2 rounded-full border border-gray-200 bg-white py-1.5 pl-1.5 pr-4 text-sm/6 font-medium text-gray-900 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:hover:bg-gray-700"
                    >
                      <app-agent-icon
                        [agentId]="pin.agentId"
                        [iconUrl]="pin.iconUrl"
                        [emoji]="pin.emoji"
                        [size]="28"
                      />
                      {{ pin.name }}
                    </a>
                  </li>
                }
              </ul>
            </section>
          }

          @if (featured().length) {
            <section class="mb-8">
              <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
                Featured
              </h2>
              <ul class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                @for (listing of featured(); track listing.agentId) {
                  <li>
                    <a
                      [routerLink]="['/agents', listing.agentId]"
                      class="flex h-full items-start gap-3 rounded-2xl border border-gray-200 bg-white p-4 hover:border-gray-300 hover:shadow-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-gray-600"
                    >
                      <app-agent-icon
                        [agentId]="listing.agentId"
                        [iconUrl]="listing.iconUrl"
                        [emoji]="listing.emoji"
                        [size]="52"
                      />
                      <div class="min-w-0 flex-1">
                        <p class="truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
                          {{ listing.name }}
                        </p>
                        <p class="line-clamp-2 text-sm/6 text-gray-500 dark:text-gray-400">
                          {{ listing.tagline || listing.publisher?.label || '' }}
                        </p>
                      </div>
                    </a>
                  </li>
                }
              </ul>
            </section>
          }

          @for (shelf of shelves(); track shelf.category.id) {
            <section class="mb-8">
              <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
                {{ shelf.category.label }}
              </h2>
              <ul class="grid gap-x-8 sm:grid-cols-2">
                @for (listing of shelf.listings; track listing.agentId) {
                  <li>
                    <app-agent-listing-row [listing]="listing" />
                  </li>
                }
              </ul>
            </section>
          }
        }
      </div>
    </div>

  `,
})
export class AgentDiscoverPage implements OnInit {
  private store = inject(AgentStoreService);
  private pinService = inject(AgentPinService);

  readonly shelves = signal<CategoryShelf[]>([]);
  readonly featured = signal<AgentListing[]>([]);
  readonly query = signal('');
  readonly loading = this.store.loading;
  readonly error = this.store.error;

  readonly pinned = this.pinService.pins;
  readonly pinError = this.pinService.error;
  readonly pinnedStrip = computed(() => this.pinned().slice(0, PINNED_STRIP_LIMIT));

  /** Everything loaded, flattened — the corpus search filters over. */
  private readonly allListings = computed(() =>
    this.shelves().flatMap((shelf) => shelf.listings),
  );

  readonly searchResults = computed(() => {
    const needle = this.query().trim().toLowerCase();
    if (!needle) return [];
    return this.allListings().filter((listing) =>
      [listing.name, listing.tagline, listing.publisher?.label]
        .filter(Boolean)
        .some((field) => (field as string).toLowerCase().includes(needle)),
    );
  });

  readonly isEmpty = computed(() =>
    this.query() ? this.searchResults().length === 0 : this.shelves().length === 0,
  );

  ngOnInit(): void {
    void this.load();
    // Independent of the shelves: every row asks the pin service whether it is pinned,
    // and a failure there must not keep the store from rendering.
    void this.pinService.load();
  }

  async load(): Promise<void> {
    try {
      const { featured, shelves } = await this.store.loadDiscover();
      this.featured.set(featured);
      this.shelves.set(shelves);
    } catch {
      // The service already captured a user-facing message in `error`.
    }
  }

  onSearch(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }
}
