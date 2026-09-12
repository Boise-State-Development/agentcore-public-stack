import { Component, ChangeDetectionStrategy, inject, input, output, signal, computed, effect } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroCheck, heroChevronDown, heroChevronRight, heroArrowPath, heroArrowLeft, heroLockClosed, heroMagnifyingGlass } from '@ng-icons/heroicons/outline';
import { ToolDetailComponent } from './tool-detail/tool-detail.component';
import {
  ConnectionState,
  ConnectorStatusService,
} from '../../settings/connectors/services/connector-status.service';
import { ModelService } from '../../session/services/model/model.service';
import { ToolService, Tool } from '../../services/tool/tool.service';
import { SkillService } from '../../services/skill/skill.service';
import { SystemPromptsService } from '../../services/system-prompts/system-prompts.service';

/**
 * One line of the tools picker — a group header or a tool row. Headers and rows
 * share one list so the template loops once rather than repeating the split-row
 * markup per group.
 */
export type ToolPickerRow =
  | {
      kind: 'header';
      id: string;
      label: string;
      count: number;
      collapsible: boolean;
      expanded: boolean;
    }
  | { kind: 'tool'; id: string; tool: Tool };

@Component({
  selector: 'app-model-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ToolDetailComponent],
  providers: [provideIcons({ heroXMark, heroCheck, heroChevronDown, heroChevronRight, heroArrowPath, heroArrowLeft, heroLockClosed, heroMagnifyingGlass })],
  host: {
    '(document:keydown.escape)': 'onEscape($event)',
  },
  templateUrl: './model-settings.html',
  styleUrl: './model-settings.css',
})
export class ModelSettings {
  protected modelService = inject(ModelService);
  protected toolService = inject(ToolService);
  protected skillService = inject(SkillService);
  protected systemPromptsService = inject(SystemPromptsService);
  protected connectorStatus = inject(ConnectorStatusService);

  // Input to control visibility
  isOpen = input<boolean>(false);

  // Session ID needed to persist prompt selection
  sessionId = input<string | null>(null);

  // Track if panel has ever been opened to avoid initial animation
  protected hasBeenOpened = signal(false);

  protected isToolsOpen = signal(false);
  protected isSkillsOpen = signal(false);

  // Output event when panel should close
  closed = output<void>();

  constructor() {
    // Track when panel is first opened and manage body scroll
    effect(() => {
      const isOpen = this.isOpen();

      if (isOpen && !this.hasBeenOpened()) {
        this.hasBeenOpened.set(true);
      }

      // Closing the drawer resets it to the list. Reopening onto whichever tool
      // you last inspected would be a surprise — the drawer's job on open is to
      // show what this conversation is carrying.
      if (!isOpen) {
        this.detailToolId.set(null);
      }

      // Probe connection state for the OAuth-gated tools this user can see, so
      // the rows can say whether a tool will actually work. `ensure` skips
      // providers it already knows, so reopening the drawer costs nothing.
      if (isOpen) {
        const providers = this.toolService
          .visibleTools()
          .map((tool) => tool.requiresOauthProvider);
        void this.connectorStatus.ensure(providers);
      }

      // Load the skills picker lazily on first open. SkillService deliberately
      // has no constructor load (unlike ToolService): skills are opt-in and the
      // feature is off in every deployed env until PR-5, so a boot-time fetch
      // would be a guaranteed 404 for every user. `initialized` is set even on
      // that 404, so this probes at most once per session.
      if (isOpen && !this.skillService.initialized()) {
        void this.skillService.loadSkills();
      }

      // Prevent background scrolling when panel is open
      if (isOpen) {
        document.body.style.overflow = 'hidden';
      } else {
        document.body.style.overflow = '';
      }
    });
  }

  close(): void {
    this.closed.emit();
  }

  /**
   * The tool whose detail pane is open, or null for the list.
   *
   * Held as an id rather than the object so the pane re-reads the live tool from
   * the service — toggling a sub-tool replaces the object in `tools()`, and a
   * captured reference would show stale switches.
   */
  private readonly detailToolId = signal<string | null>(null);

  /**
   * The last tool the detail pane showed. Kept after Back so the pane still has
   * something to render on the way out; clearing it would make the detail
   * disappear instantly instead of sliding.
   */
  protected readonly lastDetailTool = computed<Tool | null>(() => {
    const id = this.lastDetailToolId();
    return id ? (this.toolService.tools().find((t) => t.toolId === id) ?? null) : null;
  });
  private readonly lastDetailToolId = signal<string | null>(null);

  /** The open tool, or null when the list is showing. Drives the transforms. */
  protected readonly detailTool = computed<Tool | null>(() => {
    const id = this.detailToolId();
    return id ? (this.toolService.tools().find((t) => t.toolId === id) ?? null) : null;
  });

  /** Free-text filter over the tool list. */
  protected readonly toolQuery = signal('');

  /** Which category groups are expanded. Empty = all collapsed. */
  private readonly expandedCategories = signal<Set<string>>(new Set());

  /**
   * Human labels for the catalog's `category` values. The catalog carries a
   * category on every record and the drawer never used it; these are the same
   * slugs the admin tool form offers.
   *
   * An unmapped slug falls back to the raw value rather than being dropped —
   * an admin can introduce a category without a frontend release, and a tool
   * that vanished from the picker would be a far worse bug than an ugly header.
   */
  private static readonly CATEGORY_LABELS: Record<string, string> = {
    browser: 'Browser',
    code: 'Code & repositories',
    custom: 'Custom',
    data: 'Data & spreadsheets',
    document: 'Documents',
    finance: 'Finance',
    gateway: 'Gateway',
    research: 'Research',
    search: 'Search',
    utility: 'Utility',
    visualization: 'Diagrams & charts',
  };

  categoryLabel(category: string): string {
    return ModelSettings.CATEGORY_LABELS[category] ?? category;
  }

  /**
   * Which of a server's own tools matched the query. Surfacing this is the
   * difference between search that works and search that looks broken: typing
   * "calendar" matches Google Calendar's `suggest_time`, and without this the
   * row gives no hint why it is in the results.
   */
  matchedSubTools(tool: Tool): string[] {
    const query = this.toolQuery().trim().toLowerCase();
    if (!query) return [];
    // Already explained by the row's own text — don't repeat it underneath.
    if (`${tool.displayName} ${tool.description}`.toLowerCase().includes(query)) return [];
    return (tool.serverTools ?? [])
      .filter((sub) => sub.name.toLowerCase().includes(query))
      .map((sub) => sub.name)
      .slice(0, 3);
  }

  private matches(tool: Tool, query: string): boolean {
    if (!query) return true;
    const haystack = [
      tool.displayName,
      tool.description,
      tool.toolId,
      ...(tool.serverTools ?? []).map((sub) => sub.name),
    ]
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  }

  /** Everything the picker may show, after the query. */
  private readonly matchingTools = computed(() => {
    const query = this.toolQuery().trim().toLowerCase();
    return this.toolService.visibleTools().filter((tool) => this.matches(tool, query));
  });

  /** True while a query is narrowing the list. */
  protected readonly isSearching = computed(() => this.toolQuery().trim().length > 0);

  private byName = (a: Tool, b: Tool) => a.displayName.localeCompare(b.displayName);

  /**
   * Headers and rows flattened into one list, so the template keeps a single
   * loop instead of repeating the split-row markup for results, the enabled
   * group, and every category.
   *
   * Searching flattens to one ungrouped "Results" run: with a query the user is
   * looking for a specific tool, and making them expand the right category to
   * find it would defeat the search.
   */
  protected readonly toolRows = computed<ToolPickerRow[]>(() => {
    const tools = this.matchingTools();
    const rows: ToolPickerRow[] = [];

    if (this.isSearching()) {
      // No header for an empty result set — "RESULTS 0" above blank space says
      // less than the empty state does, and the template keys that state off
      // this list being empty.
      if (tools.length === 0) return rows;
      rows.push({
        kind: 'header',
        id: 'results',
        label: 'Results',
        count: tools.length,
        collapsible: false,
        expanded: true,
      });
      for (const tool of [...tools].sort(this.byName)) {
        rows.push({ kind: 'tool', id: tool.toolId, tool });
      }
      return rows;
    }

    const enabled = tools.filter((tool) => this.toolService.isToolShownEnabled(tool));
    if (enabled.length > 0) {
      rows.push({
        kind: 'header',
        id: 'enabled',
        label: 'On in this conversation',
        count: enabled.length,
        collapsible: false,
        expanded: true,
      });
      for (const tool of [...enabled].sort(this.byName)) {
        rows.push({ kind: 'tool', id: tool.toolId, tool });
      }
    }

    const groups = new Map<string, Tool[]>();
    for (const tool of tools) {
      if (this.toolService.isToolShownEnabled(tool)) continue;
      groups.set(tool.category, [...(groups.get(tool.category) ?? []), tool]);
    }

    const sorted = [...groups.entries()]
      .map(([category, list]) => ({ category, label: this.categoryLabel(category), list }))
      .sort((a, b) => a.label.localeCompare(b.label));

    for (const group of sorted) {
      const expanded = this.isCategoryExpanded(group.category);
      rows.push({
        kind: 'header',
        id: group.category,
        label: group.label,
        count: group.list.length,
        collapsible: true,
        expanded,
      });
      if (!expanded) continue;
      for (const tool of [...group.list].sort(this.byName)) {
        rows.push({ kind: 'tool', id: tool.toolId, tool });
      }
    }

    return rows;
  });

  isCategoryExpanded(category: string): boolean {
    return this.expandedCategories().has(category);
  }

  toggleCategory(category: string): void {
    this.expandedCategories.update((set) => {
      const next = new Set(set);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  }

  /**
   * Connection state for a tool's OAuth provider, or 'none' when the tool
   * needs no connection at all — most tools, so the row draws no chip.
   */
  connectionState(tool: Tool): ConnectionState | 'none' {
    if (!tool.requiresOauthProvider) return 'none';
    return this.connectorStatus.stateFor(tool.requiresOauthProvider);
  }

  clearToolQuery(): void {
    this.toolQuery.set('');
  }

  onToolQueryInput(event: Event): void {
    this.toolQuery.set((event.target as HTMLInputElement).value);
  }

  toggleTool(toolId: string): void {
    this.toolService.toggleTool(toolId);
  }

  /** True when a tool is an MCP server that supports per-tool enablement. */
  isMcpServer(tool: Tool): boolean {
    return tool.protocol === 'mcp' || tool.protocol === 'mcp_external';
  }

  /**
   * The row's one subtitle line. Leads with the count for an MCP server, and
   * with the partial-selection state when only some of its tools are on —
   * that's the fact a row can't afford to bury now that the per-tool switches
   * live a pane away.
   */
  subtitle(tool: Tool): string {
    const subs = tool.serverTools ?? [];
    const description = tool.description || 'No description recorded.';
    if (subs.length === 0) return description;
    const on = subs.filter((s) => s.enabled).length;
    const prefix =
      on > 0 && on < subs.length
        ? `${on} of ${subs.length} tools on`
        : `${subs.length} tools`;
    return `${prefix} · ${description}`;
  }

  /** Initials, so a row reads as an object rather than a line of text. */
  monogram(tool: Tool): string {
    const initials = tool.displayName
      .replace(/[^A-Za-z ]/g, '')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((word) => word[0])
      .join('');
    return initials.toUpperCase() || '?';
  }

  openToolDetail(toolId: string): void {
    this.detailToolId.set(toolId);
    this.lastDetailToolId.set(toolId);
  }

  closeToolDetail(): void {
    this.detailToolId.set(null);
  }

  /**
   * Escape backs out of the detail before it closes the drawer, so the key does
   * the least destructive thing available.
   */
  onEscape(event: Event): void {
    if (this.detailToolId() !== null) {
      event.preventDefault();
      this.closeToolDetail();
    }
  }

  selectPrompt(promptId: string | null): void {
    const sid = this.sessionId();
    this.systemPromptsService.setActivePrompt(sid, promptId)
      .catch(err => console.error('Failed to persist prompt selection:', err));
  }

  toggleTools(): void {
    this.isToolsOpen.update((open) => !open);
  }

  toggleSkills(): void {
    this.isSkillsOpen.update((open) => !open);
  }

  toggleSkill(skillId: string): void {
    this.skillService.toggleSkill(skillId)
      .catch(err => console.error('Failed to toggle skill:', err));
  }

}
