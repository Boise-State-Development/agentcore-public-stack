import { Component, ChangeDetectionStrategy } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/**
 * The `/agents` hub tab strip.
 *
 * Phase 2 has two tabs. **Pinned** joins in Phase 5 — deliberately not rendered now,
 * because a tab that leads to an empty page teaches people not to click tabs.
 */
@Component({
  selector: 'app-agents-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav
      class="inline-flex gap-1 rounded-2xl border border-gray-200 bg-gray-50 p-1 dark:border-gray-700 dark:bg-gray-800"
      aria-label="Agent views"
    >
      <a
        routerLink="/agents/discover"
        routerLinkActive="bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white"
        class="rounded-xl px-4 py-1.5 text-sm/6 font-medium text-gray-600 transition-colors hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:text-white"
      >
        Discover
      </a>
      <a
        routerLink="/agents"
        [routerLinkActiveOptions]="{ exact: true }"
        routerLinkActive="bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white"
        class="rounded-xl px-4 py-1.5 text-sm/6 font-medium text-gray-600 transition-colors hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:text-white"
      >
        My Agents
      </a>
    </nav>
  `,
})
export class AgentsTabsComponent {}
