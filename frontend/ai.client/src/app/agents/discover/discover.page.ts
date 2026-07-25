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
import { AgentStoreService } from '../services/agent-store.service';
import { AgentListing, CategoryShelf } from '../models/store.model';
import { AgentsTabsComponent } from '../components/agents-tabs.component';
import { AgentListingRowComponent } from '../components/agent-listing-row.component';

/**
 * Discover — the browse surface over published Agents (D4, Phase 2).
 *
 * Rows carry an icon, a name and one line, and nothing else: no model chip, no tool or
 * skill counts, no chat counts, no runnability badge. Those numbers are still collected
 * and still surfaced — on the detail page and in admin reporting — but a store row that
 * reports its own dependency list is a spec sheet, and it scans like one.
 *
 * Search filters what has been loaded rather than issuing a query per keystroke. The
 * store is small enough that this is honest; a real full-corpus search arrives with the
 * Registry catalog, and pretending to have one now would mean silently missing results
 * past the first page.
 *
 * Not here yet, by phase: the featured row renders only once the store-front admin can
 * populate it (Phase 5), and rows are not yet clickable because the detail page is
 * Phase 3.
 */
@Component({
  selector: 'app-agent-discover',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentsTabsComponent, AgentListingRowComponent],
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

  readonly shelves = signal<CategoryShelf[]>([]);
  readonly featured = signal<AgentListing[]>([]);
  readonly query = signal('');
  readonly loading = this.store.loading;
  readonly error = this.store.error;

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
