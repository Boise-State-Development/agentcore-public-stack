import { Component, ChangeDetectionStrategy, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroCheckBadge, heroPlus } from '@ng-icons/heroicons/outline';
import { AgentListing } from '../models/store.model';
import { AgentIconComponent } from './agent-icon.component';
import { AgentPinService } from '../services/agent-pin.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * One shelf row: icon, name, one line (D4), and the `＋` that pins it (D8).
 *
 * Deliberately carries no model chip, no tool/skill counts, no chat count and no
 * runnability badge. A row's job is to make you tap; everything else lives on the detail
 * page and in admin reporting.
 *
 * The row has **two** destinations, which is why the anchor no longer wraps the whole
 * thing: tapping the body opens the detail page, tapping `＋` pins without leaving the
 * shelf. A button nested inside an anchor is invalid HTML and swallows its own activation
 * in some browsers, so the two are siblings under a shared hover container.
 *
 * Pinning is a pointer, never a fork (D8) — nothing is copied, and the Agent the user
 * reaches tomorrow is the one its author maintains.
 */
@Component({
  selector: 'app-agent-listing-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroCheck, heroCheckBadge, heroPlus })],
  template: `
    <div
      class="group flex w-full items-center gap-1 rounded-2xl pr-2 hover:bg-gray-50 dark:hover:bg-gray-800"
    >
      <a
        [routerLink]="['/agents', listing().agentId]"
        class="flex min-w-0 flex-1 items-center gap-3 rounded-2xl px-3 py-2.5 text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
      >
        <app-agent-icon
          [agentId]="listing().agentId"
          [iconUrl]="listing().iconUrl"
          [emoji]="listing().emoji"
          [size]="40"
        />
        <div class="min-w-0 flex-1">
          <p
            class="flex items-center gap-1.5 truncate text-sm/6 font-semibold text-gray-900 dark:text-white"
          >
            {{ listing().name }}
            @if (listing().publisher?.verified) {
              <ng-icon
                name="heroCheckBadge"
                class="size-4 shrink-0 text-blue-600 dark:text-blue-400"
                [appTooltip]="verifiedTooltip()"
                appTooltipPosition="top"
              />
            }
          </p>
          <p class="truncate text-sm/6 text-gray-500 dark:text-gray-400">
            {{ subtitle() }}
          </p>
        </div>
      </a>

      <button
        type="button"
        (click)="onTogglePin()"
        [disabled]="busy()"
        [appTooltip]="pinTooltip()"
        appTooltipPosition="top"
        [attr.aria-pressed]="isPinned()"
        class="grid size-8 shrink-0 place-items-center rounded-full text-gray-400 transition-colors hover:bg-gray-200 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-gray-700 dark:hover:text-white"
        [class]="
          isPinned()
            ? 'text-emerald-600 dark:text-emerald-400'
            : 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100'
        "
      >
        <ng-icon [name]="isPinned() ? 'heroCheck' : 'heroPlus'" class="size-5" />
        <span class="sr-only">{{ pinTooltip() }}</span>
      </button>
    </div>
  `,
})
export class AgentListingRowComponent {
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
