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
import { AgentStoreTileComponent } from '../components/agent-store-tile.component';
import { AgentSpotlightComponent } from '../components/agent-spotlight.component';
import { SpinnerComponent } from '../../components/spinner/spinner.component';

/**
 * Discover — the browse surface over published Agents (D4, D10, Phases 2 and 5).
 *
 * Tiles carry an icon, a name and one line, and nothing else: no model chip, no tool or
 * skill counts, no chat counts, no runnability badge. Those numbers are still collected
 * and still surfaced — on the detail page and in admin reporting — but a store tile that
 * reports its own dependency list is a spec sheet, and it scans like one.
 *
 * The page reads top-down as a storefront:
 *
 * 1. **The spotlight** — `featured[0]`, at the size of a decision. Featured is the store's
 *    only ranking lever (everything below is newest-first), so it gets the front door.
 * 2. **Your agents** — a rail of the user's own pins, here because arriving at a store you
 *    have used before and seeing none of your own choices is disorienting.
 * 3. **Featured**, when the admin curated more than one, as an ordinary shelf.
 * 4. **Category shelves**, newest-first, empty ones dropped (D10).
 *
 * Category chips filter which shelves render. They are a *jump*, not a query: everything
 * is already loaded, so filtering is instant and can never disagree with what search
 * finds. Search likewise filters what has been loaded rather than issuing a request per
 * keystroke — the store is small enough that this is honest, and a real full-corpus
 * search arrives with the Registry catalog.
 *
 * ⚠️ Shelves are a **3-up grid, deliberately not a horizontally-scrolling rail.** The rail
 * is the app-store shape and it is wrong at this corpus size: one holding three items
 * reads as a broken carousel, and with no ranking there is nothing to justify hiding
 * items off-screen. "Your agents" *is* a rail because that one list is genuinely
 * unbounded and genuinely ordered by the person reading it.
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
    AgentStoreTileComponent,
    AgentSpotlightComponent,
    SpinnerComponent,
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

        <div class="relative max-w-md">
          <ng-icon
            name="heroMagnifyingGlass"
            class="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
            aria-hidden="true"
          />
          <label for="agent-search" class="sr-only">Search agents</label>
          <input
            type="search"
            id="agent-search"
            [value]="query()"
            (input)="onSearch($event)"
            placeholder="Search agents…"
            class="block w-full rounded-full border border-gray-300 bg-white py-2.5 pl-10 pr-4 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <!-- Category chips. Hidden while searching: search already crosses every
             category, so a category filter beside it would offer two answers to one
             question. -->
        @if (!query() && categoryChips().length > 1) {
          <div class="mt-4 flex flex-wrap gap-2" role="group" aria-label="Filter by category">
            @for (chip of categoryChips(); track chip.id) {
              <button
                type="button"
                (click)="onCategory(chip.id)"
                [attr.aria-pressed]="activeCategory() === chip.id"
                class="rounded-full border px-3.5 py-1 text-sm/6 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="
                  activeCategory() === chip.id
                    ? 'border-gray-900 bg-gray-900 text-white dark:border-white dark:bg-white dark:text-gray-900'
                    : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:text-white'
               "
              >
                {{ chip.label }}
              </button>
            }
          </div>
        }

        @if (error()) {
          <div
            role="alert"
            class="mt-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ error() }}
          </div>
        }

        <!-- Separate from the store's own error: a tile's add control failing is a
             different event from the shelves failing to load, and a pin that silently
             does nothing is the worst of the three outcomes. -->
        @if (pinError()) {
          <div
            role="alert"
            class="mt-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ pinError() }}
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <app-spinner size="lg" label="Loading agents" />
          </div>
        } @else if (isEmpty()) {
          <div
            class="mt-8 rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
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
          <!-- Search collapses the spotlight and the category sections into one flat
               result list: a storefront's merchandising is an answer to "show me
               something", and the user has just asked a narrower question. -->
          <section class="mt-8">
            <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
              {{ searchResults().length }}
              {{ searchResults().length === 1 ? 'result' : 'results' }}
            </h2>
            <ul class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              @for (listing of searchResults(); track listing.agentId) {
                <li><app-agent-store-tile [listing]="listing" /></li>
              }
            </ul>
          </section>
        } @else {
          @if (isAllCategories()) {
            @if (spotlight(); as featuredAgent) {
              <div class="mt-8">
                <app-agent-spotlight [listing]="featuredAgent" />
              </div>
            }

            @if (pinnedStrip().length) {
              <section class="mt-8">
                <div class="mb-3 flex items-baseline justify-between gap-4">
                  <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                    Your agents
                  </h2>
                  <a
                    routerLink="/agents/pinned"
                    class="text-sm/6 font-semibold text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
                  >
                    See all
                  </a>
                </div>
                <!-- The one genuine rail on the page: this list is unbounded and the
                     user chose its order, so scrolling it is browsing rather than
                     hunting for what a carousel hid. -->
                <ul class="flex gap-2.5 overflow-x-auto pb-2">
                  @for (pin of pinnedStrip(); track pin.agentId) {
                    <li class="shrink-0">
                      <a
                        [routerLink]="['/agents', pin.agentId]"
                        class="flex w-64 items-center gap-3 rounded-2xl border border-gray-200 bg-white p-2.5 hover:border-gray-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-gray-600"
                      >
                        <app-agent-icon
                          [agentId]="pin.agentId"
                          [iconUrl]="pin.iconUrl"
                          [emoji]="pin.emoji"
                          [size]="40"
                        />
                        <span class="min-w-0 flex-1">
                          <span
                            class="block truncate text-sm/5 font-semibold text-gray-900 dark:text-white"
                          >
                            {{ pin.name }}
                          </span>
                          <span class="block truncate text-sm/5 text-gray-500 dark:text-gray-400">
                            {{ pin.publisher?.label || pin.tagline || '' }}
                          </span>
                        </span>
                      </a>
                    </li>
                  }
                </ul>
              </section>
            }

            <!-- Whatever else the admin curated. The spotlight took the first, and the
                 rest are an ordinary shelf rather than a second privileged row — a
                 category shelf may not carry them all, since shelves page at 12. -->
            @if (alsoFeatured().length) {
              <section class="mt-8">
                <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
                  Also featured
                </h2>
                <ul class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  @for (listing of alsoFeatured(); track listing.agentId) {
                    <li><app-agent-store-tile [listing]="listing" /></li>
                  }
                </ul>
              </section>
            }
          }

          @for (shelf of visibleShelves(); track shelf.category.id) {
            <section class="mt-8">
              <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
                {{ shelf.category.label }}
              </h2>
              <ul class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                @for (listing of shelf.listings; track listing.agentId) {
                  <li><app-agent-store-tile [listing]="listing" /></li>
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

  /** The chip id meaning "don't filter". Not a category id, so it can never collide. */
  private static readonly ALL = '';

  readonly shelves = signal<CategoryShelf[]>([]);
  readonly featured = signal<AgentListing[]>([]);
  readonly query = signal('');
  readonly activeCategory = signal(AgentDiscoverPage.ALL);
  readonly loading = this.store.loading;
  readonly error = this.store.error;

  readonly pinned = this.pinService.pins;
  readonly pinError = this.pinService.error;
  readonly pinnedStrip = computed(() => this.pinned().slice(0, PINNED_STRIP_LIMIT));

  /** The front door. Absent when an admin has curated nothing — the page then opens on
   * the user's own pins, which is a weaker but honest first screen. */
  readonly spotlight = computed<AgentListing | null>(() => this.featured()[0] ?? null);

  readonly alsoFeatured = computed(() => this.featured().slice(1));

  readonly isAllCategories = computed(() => this.activeCategory() === AgentDiscoverPage.ALL);

  /** "All" plus one chip per shelf that actually has agents on it (D10). */
  readonly categoryChips = computed(() => [
    { id: AgentDiscoverPage.ALL, label: 'All' },
    ...this.shelves().map((shelf) => ({ id: shelf.category.id, label: shelf.category.label })),
  ]);

  readonly visibleShelves = computed(() => {
    const active = this.activeCategory();
    if (active === AgentDiscoverPage.ALL) return this.shelves();
    return this.shelves().filter((shelf) => shelf.category.id === active);
  });

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
    // Independent of the shelves: every tile asks the pin service whether it is pinned,
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

  /** Tapping the active chip clears the filter, so the chips need no separate reset. */
  onCategory(categoryId: string): void {
    this.activeCategory.update((current) =>
      current === categoryId ? AgentDiscoverPage.ALL : categoryId,
    );
  }
}
