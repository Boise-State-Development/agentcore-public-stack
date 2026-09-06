import { ChangeDetectionStrategy, Component, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroCheckBadge, heroLockClosed, heroPlus } from '@ng-icons/heroicons/outline';
import { AgentListing } from '../models/store.model';
import { AgentIconComponent } from './agent-icon.component';
import { AgentPinService } from '../services/agent-pin.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * One agent on a store shelf: artwork, name, two lines, and the control that adds it.
 *
 * The tile the shelf row (`app-agent-listing-row`) grew into. It carries the same fields
 * and the same two destinations — the body opens the detail page, `＋` pins without
 * leaving the shelf — and still deliberately no model chip, no tool or skill counts, and
 * no runnability badge (D4). A store tile that reports its own dependency list is a spec
 * sheet, and it scans like one.
 *
 * Two things changed from the row, both about being a *store*:
 *
 * * **Artwork leads at 52px** rather than 40. The icon is the thing a person recognises
 *   a week later; at row size it was a bullet point.
 * * **`＋` is always visible.** The row revealed it on hover, which on a touch device
 *   means the store's primary verb does not exist. It is now a persistent control that
 *   becomes a check once pinned.
 *
 * Pinning is a pointer, never a fork (D8) — nothing is copied, and the Agent the user
 * reaches tomorrow is the one its author maintains.
 */
@Component({
  selector: 'app-agent-store-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroCheck, heroCheckBadge, heroLockClosed, heroPlus })],
  template: `
    <div
      class="flex h-full items-center gap-3 rounded-2xl border border-gray-200 bg-white py-3 pl-3.5 pr-3 transition-colors hover:border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-gray-600"
    >
      <!--
        A button nested inside an anchor is invalid HTML and swallows its own activation
        in some browsers, so the link and the pin control are siblings rather than nested.
      -->
      <a
        [routerLink]="['/agents', listing().agentId]"
        class="flex min-w-0 flex-1 items-center gap-3 rounded-xl text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
      >
        <app-agent-icon
          [agentId]="listing().agentId"
          [iconUrl]="listing().iconUrl"
          [emoji]="listing().emoji"
          [size]="52"
        />
        <span class="min-w-0 flex-1">
          <span
            class="flex items-center gap-1.5 text-sm/5 font-semibold text-gray-900 dark:text-white"
          >
            <span class="truncate">{{ listing().name }}</span>
            @if (listing().publisher?.verified) {
              <ng-icon
                name="heroCheckBadge"
                class="size-4 shrink-0 text-state-info-600 dark:text-state-info-400"
                [appTooltip]="verifiedTooltip()"
                appTooltipPosition="top"
              />
            }
          </span>
          <span class="mt-0.5 line-clamp-2 text-sm/5 text-gray-500 dark:text-gray-400">
            {{ subtitle() }}
          </span>
        </span>
      </a>

      @if (isLocked()) {
        <!--
          A locked role pin (D9.4): the control is *absent*, not disabled. A disabled
          toggle invites a click and then refuses it; a lock says who decided.
        -->
        <span
          class="grid size-8 shrink-0 place-items-center rounded-full text-gray-400 dark:text-gray-500"
          [appTooltip]="lockTooltip()"
          appTooltipPosition="top"
        >
          <ng-icon name="heroLockClosed" class="size-4" aria-hidden="true" />
          <span class="sr-only">{{ lockTooltip() }}</span>
        </span>
      } @else {
        <button
          type="button"
          (click)="onTogglePin()"
          [disabled]="busy()"
          [appTooltip]="pinTooltip()"
          appTooltipPosition="top"
          [attr.aria-pressed]="isPinned()"
          class="grid size-8 shrink-0 place-items-center rounded-full border transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          [class]="
            isPinned()
              ? 'border-state-success-200 bg-state-success-50 text-state-success-700 dark:border-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-300'
              : 'border-gray-300 bg-white text-gray-500 hover:bg-gray-50 hover:text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white'
         "
        >
          <ng-icon [name]="isPinned() ? 'heroCheck' : 'heroPlus'" class="size-4" />
          <span class="sr-only">{{ pinTooltip() }}</span>
        </button>
      }
    </div>
  `,
  styles: [':host { display: block; height: 100%; }'],
})
export class AgentStoreTileComponent {
  private pinService = inject(AgentPinService);

  readonly listing = input.required<AgentListing>();

  readonly busy = signal(false);

  /** The tagline is the subtitle; the publisher is the fallback when there isn't one. */
  subtitle(): string {
    return this.listing().tagline || this.listing().publisher?.label || '';
  }

  isPinned(): boolean {
    return this.pinService.isPinned(this.listing().agentId);
  }

  /** Locked by a role (D9.4) — the tile keeps it and offers no way to remove it. */
  isLocked(): boolean {
    return this.pinService.isLocked(this.listing().agentId);
  }

  lockTooltip(): string {
    return `${this.listing().name} is pinned for your role and can't be removed`;
  }

  /**
   * The pinned state is a check rather than a filled `＋`, and the control stays visible
   * once pinned: an affordance that vanishes on success leaves the user unsure whether
   * anything happened, and there would be no way back.
   */
  pinTooltip(): string {
    return this.isPinned()
      ? `Remove ${this.listing().name} from your agents`
      : `Add ${this.listing().name} to your agents`;
  }

  verifiedTooltip(): string {
    const label = this.listing().publisher?.label ?? 'a university team';
    return `Published by ${label} — a verified university team`;
  }

  async onTogglePin(): Promise<void> {
    this.busy.set(true);
    try {
      await this.pinService.toggle(this.listing().agentId);
    } catch {
      // The service holds the user-facing message and has already rolled back.
    } finally {
      this.busy.set(false);
    }
  }
}
