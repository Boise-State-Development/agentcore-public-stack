import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroCheckBadge, heroLockClosed, heroPlus } from '@ng-icons/heroicons/outline';
import { AgentListing } from '../models/store.model';
import { AgentIconComponent, gradientFor } from './agent-icon.component';
import { AgentPinService } from '../services/agent-pin.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * The store's front door: one featured Agent, at the size of a decision.
 *
 * Featured is the marketplace's **only ranking lever** — `GSI5_SK` is `created_at`, so
 * every shelf below is newest-first and nothing else in the store can promote anything.
 * Rendering that lever as one more card in a row of cards spent it for nothing, which is
 * what Discover did until now.
 *
 * ⚠️ The band is tinted by **the featured agent's own tile gradient**, drawn by the same
 * `gradientFor(agentId)` every other surface uses. The front door therefore changes
 * character with whatever is on it, and can never clash with the artwork sitting in it.
 *
 * ⚠️ The scrim over that gradient is **load-bearing, not decoration**. The twelve
 * gradients were tuned to keep a white-ish *emoji* legible at tile size; two of them
 * (amber→orange, lime→green) put white *body text* under 4.5:1. A fixed scrim makes the
 * contrast a property of the component rather than of which agent an admin featured, so
 * no future palette entry can silently break this band. Don't remove it to "let the
 * colour through" — reduce the palette's lightness instead.
 */
@Component({
  selector: 'app-agent-spotlight',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroCheck, heroCheckBadge, heroLockClosed, heroPlus })],
  template: `
    @if (listing(); as l) {
      <section
        class="relative overflow-hidden rounded-3xl px-6 py-7 sm:px-8"
        [style.background-image]="background()"
        [attr.aria-label]="'Featured agent: ' + l.name"
      >
        <div
          class="relative flex flex-wrap items-center gap-x-8 gap-y-6 sm:flex-nowrap"
        >
          <div class="min-w-0 flex-1">
            <p class="text-xs/5 font-bold uppercase tracking-[0.16em] text-white/80">Featured</p>

            <h2 class="mt-2 flex flex-wrap items-center gap-2">
              <a
                [routerLink]="['/agents', l.agentId]"
                class="text-2xl/8 font-bold tracking-tight text-white hover:underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-white"
              >
                {{ l.name }}
              </a>
              @if (l.publisher?.verified) {
                <ng-icon
                  name="heroCheckBadge"
                  class="size-5 shrink-0 text-white"
                  [appTooltip]="verifiedTooltip()"
                  appTooltipPosition="top"
                />
              }
            </h2>

            @if (l.tagline) {
              <p class="mt-1.5 max-w-prose text-sm/6 text-white/90">{{ l.tagline }}</p>
            }
            @if (l.publisher?.label) {
              <p class="mt-2 text-sm/6 text-white/75">{{ l.publisher?.label }}</p>
            }

            <div class="mt-5 flex flex-wrap items-center gap-2">
              <button
                type="button"
                (click)="onStartChat()"
                class="rounded-full bg-white px-5 py-2 text-sm/6 font-semibold text-gray-900 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
              >
                Start chat
              </button>

              @if (isLocked()) {
                <span
                  class="inline-flex items-center gap-1.5 rounded-full border border-white/45 px-4 py-2 text-sm/6 font-semibold text-white"
                  [appTooltip]="lockTooltip()"
                  appTooltipPosition="top"
                >
                  <ng-icon name="heroLockClosed" class="size-4" aria-hidden="true" />
                  Added by your role
                </span>
              } @else {
                <button
                  type="button"
                  (click)="onTogglePin()"
                  [disabled]="busy()"
                  [appTooltip]="pinTooltip()"
                  appTooltipPosition="top"
                  [attr.aria-pressed]="isPinned()"
                  class="inline-flex items-center gap-1.5 rounded-full border border-white/45 px-4 py-2 text-sm/6 font-semibold text-white hover:bg-white/15 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ng-icon
                    [name]="isPinned() ? 'heroCheck' : 'heroPlus'"
                    class="size-4"
                    aria-hidden="true"
                  />
                  {{ isPinned() ? 'Added' : 'Add' }}
                </button>
              }
            </div>
          </div>

          <div class="shrink-0">
            <app-agent-icon
              [agentId]="l.agentId"
              [iconUrl]="l.iconUrl"
              [emoji]="l.emoji"
              [size]="84"
            />
          </div>
        </div>
      </section>
    }
  `,
  styles: [':host { display: block; }'],
})
export class AgentSpotlightComponent {
  private pinService = inject(AgentPinService);
  private router = inject(Router);

  readonly listing = input.required<AgentListing | null>();

  readonly busy = signal(false);

  /**
   * The agent's own gradient under a fixed scrim — see the contrast note on the class.
   * Two layers in one `background-image` so the band needs no absolutely-positioned
   * overlay element between itself and its content.
   */
  readonly background = computed(() => {
    const l = this.listing();
    if (!l) return '';
    return `linear-gradient(rgba(2, 6, 23, 0.6), rgba(2, 6, 23, 0.74)), ${gradientFor(l.agentId)}`;
  });

  isPinned(): boolean {
    return this.pinService.isPinned(this.listing()?.agentId ?? '');
  }

  isLocked(): boolean {
    return this.pinService.isLocked(this.listing()?.agentId ?? '');
  }

  lockTooltip(): string {
    return `${this.listing()?.name} is pinned for your role and can't be removed`;
  }

  pinTooltip(): string {
    const name = this.listing()?.name ?? 'this agent';
    return this.isPinned()
      ? `Remove ${name} from your agents`
      : `Add ${name} to your agents — a pointer, not a copy`;
  }

  verifiedTooltip(): string {
    const label = this.listing()?.publisher?.label ?? 'a university team';
    return `Published by ${label} — a verified university team`;
  }

  /**
   * Reuses the existing chat entry point — `assistantId` on the root route — because
   * `agentId === assistantId` and a store-launched run is an ordinary run.
   *
   * Deliberately not gated on runnability: the shelf projection carries no such answer,
   * and one extra request per page load to grey out a button would trade the store's
   * first paint for a detail the detail page already gives.
   */
  onStartChat(): void {
    const l = this.listing();
    if (!l) return;
    void this.router.navigate(['/'], { queryParams: { assistantId: l.agentId } });
  }

  async onTogglePin(): Promise<void> {
    const l = this.listing();
    if (!l) return;
    this.busy.set(true);
    try {
      await this.pinService.toggle(l.agentId);
    } catch {
      // The service holds the user-facing message and has already rolled back.
    } finally {
      this.busy.set(false);
    }
  }
}
