import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/**
 * The `/customize` hub tab strip.
 *
 * Two tabs in PR-1: **Tools** and **Skills**. **Connectors** joins them in step 2
 * when `Settings → Connectors` folds in — connecting an external MCP server and
 * enabling its tools are one user intent split across two pages today, and that
 * split gets worse, not better, once Tools lives here.
 *
 * Mirrors `AgentsTabsComponent` deliberately: `/agents` and `/customize` are
 * sibling hubs, and a second tab idiom would make them read as unrelated
 * products.
 */
@Component({
  selector: 'app-customize-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav
      class="inline-flex gap-1 rounded-2xl border border-gray-200 bg-gray-50 p-1 dark:border-gray-700 dark:bg-gray-800"
      aria-label="Customize views"
    >
      <a
        routerLink="/customize/tools"
        routerLinkActive="bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white"
        class="rounded-xl px-4 py-1.5 text-sm/6 font-medium text-gray-600 transition-colors hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
      >
        Tools
      </a>
      <a
        routerLink="/customize/skills"
        routerLinkActive="bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white"
        class="rounded-xl px-4 py-1.5 text-sm/6 font-medium text-gray-600 transition-colors hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
      >
        Skills
      </a>
    </nav>
  `,
})
export class CustomizeTabsComponent {}
