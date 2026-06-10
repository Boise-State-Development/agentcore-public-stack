import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroMagnifyingGlass,
  heroChevronRight,
  heroChevronDown,
  heroArrowPath,
} from '@ng-icons/heroicons/outline';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import { TOOL_CATEGORIES, AdminTool } from '../../tools/models/admin-tool.model';
import { makeScopedToolId, parseScopedToolId } from '../../../shared/utils/scoped-tool-id';

/**
 * Data passed to the tool picker dialog: the currently-bound tool IDs (may be
 * bare catalog ids or scoped `toolId::mcpToolName` ids).
 */
export interface ToolPickerDialogData {
  selectedToolIds: string[];
}

/**
 * Result: the chosen tool IDs if confirmed, or undefined if cancelled.
 */
export type ToolPickerDialogResult = string[] | undefined;

/** A single tool exposed by an MCP server (curated or discovered live). */
interface ServerTool {
  name: string;
  description?: string | null;
}

@Component({
  selector: 'app-tool-picker-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroXMark,
      heroMagnifyingGlass,
      heroChevronRight,
      heroChevronDown,
      heroArrowPath,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative flex max-h-[80vh] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tool-picker-title"
        aria-describedby="tool-picker-description"
      >
        <!-- Header -->
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="tool-picker-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Bind Tools
            </h2>
            <p id="tool-picker-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Select the catalog tools this skill carries. For an MCP server, expand it to bind only
              the tools you need.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <!-- Search -->
        <div class="px-6 pt-4">
          <div class="relative">
            <ng-icon
              name="heroMagnifyingGlass"
              class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <label for="tool-search" class="sr-only">Search tools</label>
            <input
              id="tool-search"
              type="text"
              [ngModel]="searchQuery()"
              (ngModelChange)="searchQuery.set($event)"
              placeholder="Search by name, ID, category or protocol…"
              class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            />
          </div>
          <div class="mt-2 flex items-center justify-between text-xs/5">
            <span class="text-gray-500 dark:text-gray-400">
              {{ selectedCountLabel() }}
            </span>
            @if (selectedToolIds().size > 0) {
              <button
                type="button"
                (click)="clearAll()"
                class="font-medium text-gray-500 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:text-white"
              >
                Clear
              </button>
            }
          </div>
        </div>

        <!-- Content -->
        <div class="mt-2 flex-1 overflow-y-auto px-6 py-2">
          @if (toolsResource.isLoading()) {
            <p class="py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">Loading tools…</p>
          } @else if (filteredTools().length === 0) {
            <p class="py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">
              No active tools match the search.
            </p>
          } @else {
            <div class="space-y-2">
              @for (tool of filteredTools(); track tool.toolId) {
                <div
                  class="rounded-2xl border border-gray-200 dark:border-gray-700"
                  [class.border-blue-500]="serverState(tool) !== 'none'"
                  [class.bg-blue-50]="serverState(tool) !== 'none'"
                  [class.dark:border-blue-400]="serverState(tool) !== 'none'"
                  [class.dark:bg-blue-900/20]="serverState(tool) !== 'none'"
                >
                  <!-- Server / tool row -->
                  <div class="flex items-center gap-2 p-3">
                    @if (isMcpServer(tool)) {
                      <button
                        type="button"
                        (click)="toggleExpanded(tool.toolId)"
                        [attr.aria-label]="expanded().has(tool.toolId) ? 'Collapse' : 'Expand'"
                        [attr.aria-expanded]="expanded().has(tool.toolId)"
                        class="flex size-6 shrink-0 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon
                          [name]="expanded().has(tool.toolId) ? 'heroChevronDown' : 'heroChevronRight'"
                          class="size-4"
                          aria-hidden="true"
                        />
                      </button>
                    } @else {
                      <span class="size-6 shrink-0"></span>
                    }

                    <input
                      type="checkbox"
                      [checked]="serverState(tool) === 'all'"
                      [indeterminate]="serverState(tool) === 'partial'"
                      (change)="toggleServer(tool)"
                      [attr.aria-label]="'Bind ' + tool.displayName"
                      class="size-4 shrink-0 rounded border-gray-300 text-blue-600 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700"
                    />

                    <div class="min-w-0 flex-1">
                      <div class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ tool.displayName }}
                      </div>
                      <div class="truncate font-mono text-xs/5 text-gray-500 dark:text-gray-400">
                        {{ tool.toolId }}
                        @if (isMcpServer(tool) && serverState(tool) === 'partial') {
                          · {{ selectedScopedNames(tool.toolId).length }} of
                          {{ serverToolsFor(tool).length || '?' }} tools
                        }
                      </div>
                    </div>

                    <span class="hidden shrink-0 rounded-2xl bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium capitalize text-gray-600 sm:inline-block dark:bg-gray-700 dark:text-gray-300">
                      {{ categoryLabel(tool.category) }}
                    </span>
                  </div>

                  <!-- Expanded sub-tools -->
                  @if (isMcpServer(tool) && expanded().has(tool.toolId)) {
                    <div class="border-t border-gray-200 px-3 py-2 pl-11 dark:border-gray-700">
                      @if (serverToolsFor(tool).length > 0) {
                        <div class="space-y-1.5">
                          @for (sub of serverToolsFor(tool); track sub.name) {
                            <label class="flex cursor-pointer items-start gap-2.5 rounded-lg px-1 py-1 hover:bg-gray-50 dark:hover:bg-gray-700/40">
                              <input
                                type="checkbox"
                                [checked]="isSubToolSelected(tool, sub.name)"
                                (change)="toggleSubTool(tool, sub.name)"
                                class="mt-0.5 size-4 shrink-0 rounded border-gray-300 text-blue-600 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700"
                              />
                              <span class="min-w-0">
                                <span class="block truncate font-mono text-xs/5 text-gray-700 dark:text-gray-200">{{ sub.name }}</span>
                                @if (sub.description) {
                                  <span class="block truncate text-xs/5 text-gray-500 dark:text-gray-400">{{ sub.description }}</span>
                                }
                              </span>
                            </label>
                          }
                        </div>
                      } @else {
                        <!-- No curated list — offer live discovery -->
                        <div class="flex flex-col gap-1.5 py-1">
                          <button
                            type="button"
                            (click)="discover(tool)"
                            [disabled]="discovering().has(tool.toolId)"
                            class="inline-flex w-fit items-center gap-1.5 rounded-lg px-2 py-1 text-xs/5 font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-50 dark:text-blue-300 dark:hover:bg-blue-900/30"
                          >
                            <ng-icon
                              name="heroArrowPath"
                              class="size-3.5"
                              [class.animate-spin]="discovering().has(tool.toolId)"
                              aria-hidden="true"
                            />
                            {{ discovering().has(tool.toolId) ? 'Discovering…' : 'Discover tools' }}
                          </button>
                          @if (discoverError()[tool.toolId]) {
                            <span class="text-xs/5 text-red-600 dark:text-red-400">{{ discoverError()[tool.toolId] }}</span>
                          } @else {
                            <span class="text-xs/5 text-gray-500 dark:text-gray-400">
                              This server's tools aren't listed in the catalog. Discover them to bind a subset,
                              or bind the whole server with the checkbox above.
                            </span>
                          }
                        </div>
                      }
                    </div>
                  }
                </div>
              }
            </div>
          }
        </div>

        <!-- Actions -->
        <div class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="confirm()"
            class="inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }

    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.97);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `,
})
export class ToolPickerDialogComponent {
  protected readonly dialogRef = inject(DialogRef<ToolPickerDialogResult>);
  protected readonly data = inject<ToolPickerDialogData>(DIALOG_DATA);

  private adminToolService = inject(AdminToolService);

  readonly toolsResource = this.adminToolService.toolsResource;
  readonly searchQuery = signal('');
  readonly selectedToolIds = signal<Set<string>>(new Set(this.data.selectedToolIds));
  readonly expanded = signal<Set<string>>(new Set());
  readonly discovering = signal<Set<string>>(new Set());
  readonly discovered = signal<Record<string, ServerTool[]>>({});
  readonly discoverError = signal<Record<string, string>>({});

  /**
   * Only ACTIVE tools are bindable — the backend rejects binding an unknown or
   * non-active tool (see SkillCatalogService._validate_bound_tools).
   */
  readonly activeTools = computed(() =>
    this.adminToolService.getTools().filter((t) => t.status === 'active')
  );

  readonly filteredTools = computed(() => {
    const query = this.searchQuery().toLowerCase().trim();
    const tools = this.activeTools();
    const filtered = query
      ? tools.filter(
          (t) =>
            t.displayName.toLowerCase().includes(query) ||
            t.toolId.toLowerCase().includes(query) ||
            t.category.toLowerCase().includes(query) ||
            t.protocol.toLowerCase().includes(query)
        )
      : tools;
    return [...filtered].sort((a, b) => a.displayName.localeCompare(b.displayName));
  });

  readonly selectedCountLabel = computed(() => `${this.selectedToolIds().size} selected`);

  categoryLabel(category: string): string {
    return TOOL_CATEGORIES.find((c) => c.value === category)?.label ?? category;
  }

  isMcpServer(tool: AdminTool): boolean {
    return tool.protocol === 'mcp' || tool.protocol === 'mcp_external';
  }

  /** The tools a server exposes — its curated list, else any discovered live. */
  serverToolsFor(tool: AdminTool): ServerTool[] {
    const curated = tool.mcpConfig?.tools ?? tool.mcpGatewayConfig?.tools ?? [];
    if (curated.length > 0) {
      return curated.map((t) => ({ name: t.name, description: t.description }));
    }
    return this.discovered()[tool.toolId] ?? [];
  }

  /** Names of the individual tools currently selected for a given server. */
  selectedScopedNames(toolId: string): string[] {
    const names: string[] = [];
    for (const id of this.selectedToolIds()) {
      const { base, name } = parseScopedToolId(id);
      if (base === toolId && name) {
        names.push(name);
      }
    }
    return names;
  }

  /** Whole server ('all'), a subset ('partial'), or nothing ('none'). */
  serverState(tool: AdminTool): 'all' | 'partial' | 'none' {
    const set = this.selectedToolIds();
    if (set.has(tool.toolId)) {
      return 'all';
    }
    return this.selectedScopedNames(tool.toolId).length > 0 ? 'partial' : 'none';
  }

  isSubToolSelected(tool: AdminTool, name: string): boolean {
    const set = this.selectedToolIds();
    return set.has(tool.toolId) || set.has(makeScopedToolId(tool.toolId, name));
  }

  toggleExpanded(toolId: string): void {
    this.expanded.update((set) => {
      const next = new Set(set);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
  }

  /** Toggle binding the whole server (clears any per-tool subset). */
  toggleServer(tool: AdminTool): void {
    const wasOff = this.serverState(tool) === 'none';
    this.selectedToolIds.update((set) => {
      const next = this.withoutBase(set, tool.toolId);
      if (wasOff) {
        next.add(tool.toolId);
      }
      return next;
    });
  }

  /** Toggle a single tool of a server (switches the server to a subset). */
  toggleSubTool(tool: AdminTool, name: string): void {
    const scoped = makeScopedToolId(tool.toolId, name);
    this.selectedToolIds.update((set) => {
      const next = new Set(set);
      // Customizing a whole-server binding expands it into explicit per-tool
      // ids for everything currently known, then toggles the chosen one.
      if (next.has(tool.toolId)) {
        next.delete(tool.toolId);
        for (const sub of this.serverToolsFor(tool)) {
          next.add(makeScopedToolId(tool.toolId, sub.name));
        }
      }
      if (next.has(scoped)) {
        next.delete(scoped);
      } else {
        next.add(scoped);
      }
      return next;
    });
  }

  /** Toggle a non-MCP (single) tool. */
  toggleTool(toolId: string): void {
    this.selectedToolIds.update((set) => {
      const next = new Set(set);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
  }

  async discover(tool: AdminTool): Promise<void> {
    this.discovering.update((s) => new Set(s).add(tool.toolId));
    this.discoverError.update((m) => {
      const next = { ...m };
      delete next[tool.toolId];
      return next;
    });
    try {
      const res = await this.adminToolService.discoverSavedToolTools(tool.toolId);
      this.discovered.update((d) => ({
        ...d,
        [tool.toolId]: res.tools.map((t) => ({ name: t.name, description: t.description })),
      }));
      if (res.tools.length === 0) {
        this.discoverError.update((m) => ({
          ...m,
          [tool.toolId]: 'The server returned no tools.',
        }));
      }
    } catch {
      this.discoverError.update((m) => ({
        ...m,
        [tool.toolId]: 'Could not list this server’s tools. Bind the whole server instead.',
      }));
    } finally {
      this.discovering.update((s) => {
        const next = new Set(s);
        next.delete(tool.toolId);
        return next;
      });
    }
  }

  clearAll(): void {
    this.selectedToolIds.set(new Set());
  }

  confirm(): void {
    this.dialogRef.close(Array.from(this.selectedToolIds()));
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }

  /** A copy of `set` with the bare id and every scoped id for `base` removed. */
  private withoutBase(set: Set<string>, base: string): Set<string> {
    const next = new Set<string>();
    for (const id of set) {
      if (parseScopedToolId(id).base !== base) {
        next.add(id);
      }
    }
    return next;
  }
}
