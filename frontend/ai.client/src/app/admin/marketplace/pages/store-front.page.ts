import {
  Component,
  ChangeDetectionStrategy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDown,
  heroArrowUp,
  heroStar,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import {
  AdminListingRow,
  AgentListing,
  MAX_FEATURED,
} from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';

/**
 * Store front — the Featured row, as an explicitly ordered list (D10).
 *
 * This surface exists because **it is the only ranking lever the store has.** Browse is
 * newest-first (`GSI5_SK` is `created_at`) and v1 ships no popularity sort, so promotion
 * is how a good Agent gets found. That makes the ordering something a person owns, not
 * something the system infers — hence explicit slots with explicit move controls rather
 * than drag-and-drop, which is hard to operate by keyboard and impossible to describe in
 * a screen reader without a live region.
 *
 * The editor is **staged**: moves and removals change local order, and Save writes the
 * whole array in one PUT. Reordering has to be atomic — a half-applied order is a store
 * front nobody chose — and per-move autosave would make every mis-click a live change to
 * the front page of the store.
 *
 * Only published agents may be featured, and that rule is enforced server-side: the PUT
 * names any id that is not published rather than silently dropping it. `unavailable`
 * carries the same news for ids that *were* published when they were promoted and have
 * since been taken down.
 */
@Component({
  selector: 'app-marketplace-store-front',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentTileComponent, TooltipDirective],
  providers: [provideIcons({ heroArrowDown, heroArrowUp, heroStar, heroXMark })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Store front</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            The featured row at the top of Discover, in order. Everything below it is
            newest-first — there is no popularity ranking — so this is how a good agent
            gets found. Up to {{ maxFeatured }} agents.
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

        @if (unavailable().length) {
          <div
            role="status"
            class="mb-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm/6 text-amber-900 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200"
          >
            {{ unavailable().length }}
            {{ unavailable().length === 1 ? 'featured agent is' : 'featured agents are' }}
            no longer published, so
            {{ unavailable().length === 1 ? 'its slot is' : 'their slots are' }}
            empty on Discover. Saving this row clears
            {{ unavailable().length === 1 ? 'it' : 'them' }}.
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading the store front</span>
          </div>
        } @else {
          <!-- The row -->
          <section class="mb-8">
            <div class="mb-3 flex items-center justify-between gap-4">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                Featured ({{ featured().length }}/{{ maxFeatured }})
              </h2>
              <div class="flex items-center gap-2">
                @if (dirty()) {
                  <button
                    type="button"
                    (click)="reload()"
                    [disabled]="busy()"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                  >
                    Discard
                  </button>
                }
                <button
                  type="button"
                  (click)="save()"
                  [disabled]="!dirty() || busy()"
                  class="rounded-2xl bg-blue-600 px-4 py-1.5 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  {{ dirty() ? 'Save order' : 'Saved' }}
                </button>
              </div>
            </div>

            @if (featured().length === 0) {
              <div
                class="rounded-2xl border border-dashed border-gray-300 px-6 py-12 text-center dark:border-gray-600"
              >
                <ng-icon
                  name="heroStar"
                  class="mx-auto size-8 text-gray-400 dark:text-gray-500"
                  aria-hidden="true"
                />
                <h3 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
                  Nothing featured
                </h3>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  Discover renders no featured row until something is promoted below.
                </p>
              </div>
            } @else {
              <ol class="flex flex-col gap-2">
                @for (row of featured(); track row.agentId; let i = $index) {
                  <li
                    class="flex items-center gap-3 rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800"
                  >
                    <span
                      class="w-6 shrink-0 text-center text-sm/6 font-semibold tabular-nums text-gray-400 dark:text-gray-500"
                      aria-hidden="true"
                    >
                      {{ i + 1 }}
                    </span>
                    <app-agent-tile
                      [agentId]="row.agentId"
                      [iconUrl]="row.iconUrl"
                      [emoji]="row.emoji"
                      size="sm"
                    />
                    <div class="min-w-0 flex-1">
                      <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ row.name }}
                      </p>
                      <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                        {{ row.tagline || row.publisher?.label || row.category }}
                      </p>
                    </div>
                    <div class="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        (click)="moveUp(i)"
                        [disabled]="i === 0 || busy()"
                        [appTooltip]="'Move up'"
                        appTooltipPosition="top"
                        class="rounded-2xl p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-30 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
                      >
                        <ng-icon name="heroArrowUp" class="size-5" aria-hidden="true" />
                        <span class="sr-only">Move {{ row.name }} up</span>
                      </button>
                      <button
                        type="button"
                        (click)="moveDown(i)"
                        [disabled]="i === featured().length - 1 || busy()"
                        [appTooltip]="'Move down'"
                        appTooltipPosition="top"
                        class="rounded-2xl p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-30 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
                      >
                        <ng-icon name="heroArrowDown" class="size-5" aria-hidden="true" />
                        <span class="sr-only">Move {{ row.name }} down</span>
                      </button>
                      <button
                        type="button"
                        (click)="remove(row.agentId)"
                        [disabled]="busy()"
                        [appTooltip]="'Remove from the featured row'"
                        appTooltipPosition="top"
                        class="rounded-2xl p-2 text-gray-500 hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-rose-900/20 dark:hover:text-rose-400"
                      >
                        <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
                        <span class="sr-only">Remove {{ row.name }}</span>
                      </button>
                    </div>
                  </li>
                }
              </ol>
            }
          </section>

          <!-- The candidates -->
          <section>
            <h2 class="mb-1 text-base/7 font-semibold text-gray-900 dark:text-white">
              Published agents
            </h2>
            <p class="mb-3 text-sm/6 text-gray-600 dark:text-gray-400">
              Only published agents can be featured — a tile nobody can open is worse than
              a short row.
            </p>

            @if (candidates().length === 0) {
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Nothing else is published yet.
              </p>
            } @else {
              <ul class="flex flex-col gap-2">
                @for (row of candidates(); track row.agentId) {
                  <li
                    class="flex items-center gap-3 rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800"
                  >
                    <app-agent-tile
                      [agentId]="row.agentId"
                      [iconUrl]="row.iconUrl"
                      [emoji]="row.emoji"
                      size="sm"
                    />
                    <div class="min-w-0 flex-1">
                      <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ row.name }}
                      </p>
                      <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                        {{ row.tagline || row.category }}
                      </p>
                    </div>
                    <button
                      type="button"
                      (click)="promote(row)"
                      [disabled]="isFull() || busy()"
                      [appTooltip]="
                        isFull()
                          ? 'The featured row is full — remove one first'
                          : 'Add to the featured row'
                      "
                      appTooltipPosition="top"
                      class="shrink-0 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      Feature
                    </button>
                  </li>
                }
              </ul>
            }
          </section>
        }
      </div>
    </div>
  `,
})
export class MarketplaceStoreFrontPage implements OnInit {
  private service = inject(AdminMarketplaceService);

  readonly maxFeatured = MAX_FEATURED;

  readonly featured = signal<AgentListing[]>([]);
  readonly published = signal<AdminListingRow[]>([]);
  readonly unavailable = signal<string[]>([]);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  /** The saved order, to compare against. Staging is what makes reordering atomic. */
  private savedOrder = signal<string[]>([]);

  private readonly order = computed(() => this.featured().map((row) => row.agentId));

  readonly dirty = computed(
    () => this.order().join(' ') !== this.savedOrder().join(' '),
  );

  readonly isFull = computed(() => this.featured().length >= MAX_FEATURED);

  /** Published agents not already in the row — the only things that may be promoted. */
  readonly candidates = computed(() => {
    const already = new Set(this.order());
    return this.published().filter((row) => !already.has(row.agentId));
  });

  ngOnInit(): void {
    void this.reload();
  }

  async reload(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const [front, published] = await Promise.all([
        this.service.loadStoreFront(),
        this.service.loadListings('published'),
      ]);
      this.featured.set(front.featured ?? []);
      this.unavailable.set(front.unavailable ?? []);
      this.savedOrder.set((front.featured ?? []).map((row) => row.agentId));
      this.published.set(published);
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to load the store front.'));
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Promote from the published list. The row is built from the admin row rather than
   * re-fetched: the two shapes overlap on everything a tile renders, and Save round-trips
   * the authoritative version back anyway.
   */
  promote(row: AdminListingRow): void {
    if (this.isFull()) return;
    this.featured.update((current) => [
      ...current,
      {
        agentId: row.agentId,
        name: row.name,
        tagline: row.tagline,
        emoji: row.emoji,
        iconUrl: row.iconUrl,
        publisher: row.publisher
          ? {
              label: row.publisher.label,
              kind: row.publisher.kind,
              verified: row.publisher.verified,
            }
          : null,
        category: row.category,
      },
    ]);
  }

  remove(agentId: string): void {
    this.featured.update((current) => current.filter((row) => row.agentId !== agentId));
  }

  moveUp(index: number): void {
    this.swap(index, index - 1);
  }

  moveDown(index: number): void {
    this.swap(index, index + 1);
  }

  private swap(from: number, to: number): void {
    this.featured.update((current) => {
      if (to < 0 || to >= current.length) return current;
      const next = [...current];
      [next[from], next[to]] = [next[to], next[from]];
      return next;
    });
  }

  async save(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      const saved = await this.service.saveStoreFront(this.order());
      this.featured.set(saved.featured ?? []);
      this.unavailable.set(saved.unavailable ?? []);
      this.savedOrder.set((saved.featured ?? []).map((row) => row.agentId));
    } catch (err) {
      // The refusal names the offending ids, so the server's message beats a generic one.
      this.error.set(this.messageFor(err, 'Failed to save the store front.'));
    } finally {
      this.busy.set(false);
    }
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }
}
