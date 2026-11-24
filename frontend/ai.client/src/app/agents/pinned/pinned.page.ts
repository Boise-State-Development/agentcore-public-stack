import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowsUpDown, heroBookmark } from '@ng-icons/heroicons/outline';
import { AgentPinService } from '../services/agent-pin.service';
import { AgentsTabsComponent } from '../components/agents-tabs.component';
import { AgentListingRowComponent } from '../components/agent-listing-row.component';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { ArrangeListComponent, ArrangeResult, ArrangeRow } from '../../components/arrange-list/arrange-list.component';
import { FEATURES } from '../../services/features';
import { ToastService } from '../../services/toast/toast.service';

/**
 * Pinned — the Agents a user has added to their own set, plus the ones their role seeds
 * (D8, D9, Phases 5–6).
 *
 * The rows are the same shelf rows Discover renders, and their `＋` has already become a
 * check, so removing one happens here without a second control to learn. That is the
 * whole reason the pin toggle lives on the row rather than on the page: one gesture,
 * everywhere it appears — including on a role-seeded row, which a user removes exactly
 * like their own (the dismissal is remembered, so it stays gone).
 *
 * The one exception is a **locked** seed (D9.4): the row hides the control rather than
 * disabling it. That is driven by the row's own state rather than by "is this page the
 * Pinned tab", so it holds on Discover too.
 *
 * A pin whose Agent was deleted, or whose visibility narrowed, is simply absent: the
 * server resolves the list against the viewer on every read, and it does not delete the
 * underlying pin for either reason.
 */
@Component({
  selector: 'app-agent-pinned',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentsTabsComponent, AgentListingRowComponent, ArrangeListComponent, NgIcon, RouterLink, SpinnerComponent],
  // The empty state renders <ng-icon name="heroBookmark" />, and ng-icons resolves that
  // name from a registration rather than from the import. Both symbols were imported and
  // neither was ever called, so the glyph silently rendered as nothing — CodeQL surfaced
  // it as two unused imports, which is what a missing registration looks like from the
  // outside. Matches how discover.page.ts registers its own icons.
  providers: [provideIcons({ heroArrowsUpDown, heroBookmark })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <app-agents-tabs />

        <div class="mt-6 mb-10 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 class="text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl dark:text-white">Pinned agents</h1>
            <p class="mt-1.5 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
              The agents you have added, plus any your role starts you with. Removing one
              here does not affect anyone else.
            </p>
          </div>
          <!-- The order here is the sidebar's order too (spec D7). -->
          @if (features.organizeLists && movableCount() > 1 && !arranging()) {
            <button
              type="button"
              (click)="arranging.set(true)"
              class="inline-flex items-center gap-2 rounded-2xl bg-white px-3 py-2 text-sm/6 font-medium text-gray-900 ring-1 ring-gray-300 ring-inset transition hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-white/10 dark:text-white dark:ring-white/5 dark:hover:bg-white/20"
            >
              <ng-icon name="heroArrowsUpDown" class="size-4" aria-hidden="true" />
              Arrange
            </button>
          }
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
            <app-spinner size="lg" label="Loading pinned agents" />
          </div>
        } @else if (isEmpty()) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroBookmark"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              Nothing pinned yet
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Add an agent from Discover and it will wait for you here.
            </p>
            <a
              routerLink="/agents/discover"
              class="mt-4 inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2.5 text-sm/6 font-semibold text-white shadow-xs transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
            >
              Browse agents
            </a>
          </div>
        } @else if (arranging()) {
          <app-arrange-list
            label="Pinned agents"
            [items]="arrangeRows()"
            [allowHide]="false"
            (done)="onArranged($event)"
            (cancelled)="arranging.set(false)"
          />
        } @else {
          <ul class="grid gap-x-8 sm:grid-cols-2">
            @for (pin of pins(); track pin.agentId) {
              <li>
                <app-agent-listing-row [listing]="pin" />
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class AgentPinnedPage implements OnInit {
  private pinService = inject(AgentPinService);
  private toast = inject(ToastService);
  protected readonly features = inject(FEATURES);

  /** Arrange mode: the order set here is the sidebar's order too (spec D7). */
  readonly arranging = signal(false);

  readonly pins = this.pinService.pins;
  readonly loading = this.pinService.loading;
  readonly error = this.pinService.error;

  readonly isEmpty = computed(() => this.pins().length === 0);

  readonly movableCount = computed(() => this.pins().filter((pin) => !pin.locked).length);

  readonly arrangeRows = computed<ArrangeRow[]>(() =>
    this.pins().map((pin) => ({
      id: pin.agentId,
      name: pin.name,
      detail: pin.publisher?.label ?? pin.tagline ?? undefined,
      locked: pin.locked,
    })),
  );

  async onArranged(result: ArrangeResult): Promise<void> {
    this.arranging.set(false);
    try {
      await this.pinService.reorder(result.ids);
    } catch {
      this.toast.error('Could not save your order', this.pinService.error() ?? 'Please try again.');
    }
  }

  ngOnInit(): void {
    // Forced: this is the page whose whole content is the list, so a stale session-cached
    // answer is exactly the thing a user arrives here to check.
    void this.pinService.load(true);
  }
}
