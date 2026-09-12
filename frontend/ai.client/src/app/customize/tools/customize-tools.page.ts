import { ChangeDetectionStrategy, Component, computed, effect, inject, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroMagnifyingGlass } from '@ng-icons/heroicons/outline';
import { Tool, ToolService } from '../../services/tool/tool.service';
import { ConnectorStatusService } from '../../settings/connectors/services/connector-status.service';
import { splitToolDescription } from '../../shared/utils/tool-description';
import { monogramFor } from '../../shared/utils/monogram';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { CustomizeTabsComponent } from '../components/customize-tabs.component';
import {
  CustomizeCardBadge,
  CustomizeCardComponent,
} from '../components/customize-card.component';

/** A tool paired with everything the card needs, resolved once per render. */
interface ToolCard {
  tool: Tool;
  name: string;
  description: string;
  monogram: string;
  enabled: boolean;
  badge: CustomizeCardBadge;
}

/**
 * Customize → Tools. The browsable home for tool enablement (spec step 1).
 *
 * ⚠️ This page deliberately reads `toolService.tools()` and `tool.isEnabled`,
 * NOT `visibleTools()` / `isToolShownEnabled()`, and writes with
 * `respectAgentLock: false`. The Agent binding lock is conversation-scoped state
 * held on a root singleton that the session view never releases on teardown, so
 * a user arriving here from an agent-bound chat would otherwise see the Agent's
 * toolset in place of their own preferences and find every switch inert. See
 * `docs/specs/customize-surface.md` §"The agent-lock seam"; step 4 moves that
 * fact onto the assistant indicator, where it belongs.
 *
 * The browse idiom — search box, category chips, responsive grid — is borrowed
 * from `agents/discover` on purpose. Everything is already loaded, so filtering
 * is instant and search and chips can never disagree.
 */
@Component({
  selector: 'app-customize-tools',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, SpinnerComponent, CustomizeTabsComponent, CustomizeCardComponent],
  providers: [provideIcons({ heroMagnifyingGlass })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <app-customize-tabs />

        <div class="mt-6 mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Tools</h1>
          <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
            What your assistant can do. Changes apply to every conversation, including
            ones already open.
          </p>
        </div>

        <div class="relative max-w-md">
          <ng-icon
            name="heroMagnifyingGlass"
            class="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
            aria-hidden="true"
          />
          <label for="customize-tool-search" class="sr-only">Search tools</label>
          <input
            type="search"
            id="customize-tool-search"
            [value]="query()"
            (input)="onSearch($event)"
            placeholder="Search tools…"
            class="block w-full rounded-full border border-gray-300 bg-white py-2.5 pl-10 pr-4 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <!-- Category chips. Hidden while searching: search already crosses every
             category, so a category filter beside it would offer two answers to
             one question. -->
        @if (!query() && categoryChips().length > 1) {
          <div class="mt-4 flex flex-wrap gap-2" role="group" aria-label="Filter by category">
            @for (chip of categoryChips(); track chip) {
              <button
                type="button"
                (click)="onCategory(chip)"
                [attr.aria-pressed]="activeCategory() === chip"
                class="rounded-full border px-3.5 py-1 text-sm/6 font-medium capitalize transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="
                  activeCategory() === chip
                    ? 'border-gray-900 bg-gray-900 text-white dark:border-white dark:bg-white dark:text-gray-900'
                    : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:text-white'
                "
              >
                {{ chip === ALL ? 'All' : chip }}
              </button>
            }
          </div>
        }

        @if (toolService.error(); as loadError) {
          <div
            role="alert"
            class="mt-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ loadError }}
          </div>
        }

        <!-- A save that failed is a different event from a catalog that failed to
             load, and a switch that silently snaps back is the worst outcome of
             the three. -->
        @if (saveError()) {
          <div
            role="alert"
            class="mt-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ saveError() }}
          </div>
        }

        @if (toolService.loading() && !toolService.initialized()) {
          <div class="mt-8 flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
            <app-spinner size="sm" label="Loading tools" />
            Loading tools…
          </div>
        } @else if (cards().length === 0) {
          <div
            class="mt-8 rounded-2xl border border-dashed border-gray-300 p-8 text-center dark:border-gray-700"
          >
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">
              {{ query() ? 'No tools match your search' : 'No tools available' }}
            </p>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              {{
                query()
                  ? 'Try a different word, or clear the search to browse everything.'
                  : 'Your roles do not grant access to any tools yet.'
              }}
            </p>
          </div>
        } @else {
          <p class="mt-6 text-sm/6 text-gray-500 dark:text-gray-400" aria-live="polite">
            {{ enabledLabel() }}
          </p>
          <ul class="mt-3 grid gap-3 sm:grid-cols-2">
            @for (card of cards(); track card.tool.toolId) {
              <li>
                <app-customize-card
                  [name]="card.name"
                  [description]="card.description"
                  [monogram]="card.monogram"
                  [enabled]="card.enabled"
                  [badge]="card.badge"
                  [pending]="pending().has(card.tool.toolId)"
                  (toggled)="onToggle(card.tool)"
                />
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class CustomizeToolsPage {
  protected readonly toolService = inject(ToolService);
  private readonly connectorStatus = inject(ConnectorStatusService);

  /** The chip meaning "don't filter". Not a category id, so it can never collide. */
  protected readonly ALL = '__all__';

  protected readonly query = signal('');
  protected readonly activeCategory = signal<string>(this.ALL);
  protected readonly pending = signal<ReadonlySet<string>>(new Set());
  protected readonly saveError = signal<string | null>(null);

  constructor() {
    // ToolService loads in its own constructor, so the catalog is usually
    // already in flight by the time this page mounts. Ask anyway when it isn't:
    // a deep link to /customize/tools is a legitimate first paint.
    if (!this.toolService.initialized() && !this.toolService.loading()) {
      void this.toolService.loadTools();
    }

    // Resolve connection state for whichever providers the catalog actually
    // references. `ensure` de-dupes and caches, so this settles once.
    effect(() => {
      const providers = this.toolService
        .tools()
        .map(t => t.requiresOauthProvider)
        .filter(Boolean);
      if (providers.length > 0) {
        void this.connectorStatus.ensure(providers);
      }
    });
  }

  protected readonly categoryChips = computed(() => {
    const categories = [...new Set(this.toolService.tools().map(t => t.category))].sort();
    return [this.ALL, ...categories];
  });

  protected readonly cards = computed<ToolCard[]>(() => {
    const q = this.query().trim().toLowerCase();
    const category = this.activeCategory();

    // `tools()`, never `visibleTools()` — see the class comment.
    let tools = this.toolService.tools();

    if (q) {
      tools = tools.filter(tool =>
        [tool.displayName, tool.description, tool.category]
          .filter(Boolean)
          .some(field => field.toLowerCase().includes(q)),
      );
    } else if (category !== this.ALL) {
      tools = tools.filter(tool => tool.category === category);
    }

    return tools.map(tool => ({
      tool,
      name: tool.displayName,
      description: this.describe(tool),
      monogram: monogramFor(tool.displayName),
      // `isEnabled`, never `isToolShownEnabled()` — see the class comment.
      enabled: tool.isEnabled,
      badge: this.badgeFor(tool),
    }));
  });

  protected readonly enabledLabel = computed(() => {
    const total = this.toolService.tools().length;
    const on = this.toolService.tools().filter(t => t.isEnabled).length;
    return `${on} of ${total} ${total === 1 ? 'tool' : 'tools'} on`;
  });

  protected onSearch(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  protected onCategory(chip: string): void {
    this.activeCategory.set(chip);
  }

  protected async onToggle(tool: Tool): Promise<void> {
    const id = tool.toolId;
    if (this.pending().has(id)) return;

    this.saveError.set(null);
    this.pending.update(set => new Set(set).add(id));
    try {
      // `respectAgentLock: false` — see the class comment.
      await this.toolService.toggleTool(id, { respectAgentLock: false });
    } catch {
      // The service already reverted its optimistic update; say so, because a
      // switch that snaps back on its own looks like a bug in the switch.
      this.saveError.set(`Couldn't save the change to ${tool.displayName}. Please try again.`);
    } finally {
      this.pending.update(set => {
        const next = new Set(set);
        next.delete(id);
        return next;
      });
    }
  }

  /**
   * The card's one description line. Leads with the tool count for an MCP
   * server, and with the partial-selection state when only some of its tools
   * are on — PR-1 has no per-tool pane, so that is the fact a card must not
   * bury.
   */
  private describe(tool: Tool): string {
    const summary =
      splitToolDescription(tool.description).summary || 'No description recorded.';
    const subs = tool.serverTools ?? [];
    if (subs.length === 0) return summary;

    const on = subs.filter(s => s.enabled).length;
    const noun = subs.length === 1 ? 'tool' : 'tools';
    const prefix =
      on > 0 && on < subs.length
        ? `${on} of ${subs.length} ${noun} on`
        : `${subs.length} ${noun}`;
    return `${prefix} · ${summary}`;
  }

  /**
   * `unknown` draws no chip: a probe that failed is not evidence the user is
   * disconnected, and most tools need no connection at all.
   */
  private badgeFor(tool: Tool): CustomizeCardBadge {
    if (!tool.requiresOauthProvider) return null;
    const state = this.connectorStatus.stateFor(tool.requiresOauthProvider);
    if (state === 'connected') return 'connected';
    if (state === 'disconnected') return 'connect';
    return null;
  }
}
