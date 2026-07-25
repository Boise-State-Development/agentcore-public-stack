import { Component, ChangeDetectionStrategy, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheckBadge } from '@ng-icons/heroicons/outline';
import { AgentListing } from '../models/store.model';
import { AgentIconComponent } from './agent-icon.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * One shelf row: icon, name, one line (D4).
 *
 * Deliberately carries no model chip, no tool/skill counts, no chat count and no
 * runnability badge. A row's job is to make you tap; everything else lives on the detail
 * page and in admin reporting.
 *
 * The icon is `app-agent-icon` at 40px: the author's uploaded square when there is one,
 * the generated gradient carrying the emoji when there is not (D5).
 *
 * The whole row routes to the detail page (Phase 3). A `+` add affordance sits beside it
 * in the mockup; that is Phase 5's pin, so the row has exactly one destination today.
 */
@Component({
  selector: 'app-agent-listing-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroCheckBadge })],
  template: `
    <a
      [routerLink]="['/agents', listing().agentId]"
      class="flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 dark:hover:bg-gray-800"
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
