import { Component, ChangeDetectionStrategy, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheckBadge } from '@ng-icons/heroicons/outline';
import { AgentListing } from '../models/store.model';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * One shelf row: icon, name, one line (D4).
 *
 * Deliberately carries no model chip, no tool/skill counts, no chat count and no
 * runnability badge. A row's job is to make you tap; everything else lives on the detail
 * page and in admin reporting.
 *
 * The icon is the emoji on a neutral tile for now — D5's uploaded icon and generated
 * gradient fallback are Phase 4.
 */
@Component({
  selector: 'app-agent-listing-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [provideIcons({ heroCheckBadge })],
  template: `
    <div class="flex items-center gap-3 rounded-2xl px-3 py-2.5">
      <span
        class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-gray-100 text-xl dark:bg-gray-700"
        aria-hidden="true"
      >
        {{ listing().emoji || '✦' }}
      </span>
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
    </div>
  `,
})
export class AgentListingRowComponent {
  readonly listing = input.required<AgentListing>();

  /** The tagline is the subtitle; the publisher is the fallback when there isn't one. */
  subtitle(): string {
    return this.listing().tagline || this.listing().publisher?.label || '';
  }

  verifiedTooltip(): string {
    const label = this.listing().publisher?.label ?? 'a university team';
    return `Published by ${label} — a verified university team`;
  }
}
