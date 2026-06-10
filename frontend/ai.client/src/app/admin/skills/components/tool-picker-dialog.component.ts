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
import { heroXMark, heroMagnifyingGlass } from '@ng-icons/heroicons/outline';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import { TOOL_CATEGORIES } from '../../tools/models/admin-tool.model';

/**
 * Data passed to the tool picker dialog: the currently-bound tool IDs.
 */
export interface ToolPickerDialogData {
  selectedToolIds: string[];
}

/**
 * Result: the chosen tool IDs if confirmed, or undefined if cancelled.
 */
export type ToolPickerDialogResult = string[] | undefined;

@Component({
  selector: 'app-tool-picker-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [provideIcons({ heroXMark, heroMagnifyingGlass })],
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
              Select the catalog tools this skill carries. The agent loads only these tools when the skill is active.
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
              {{ selectedToolIds().size }} selected
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
                <label
                  class="flex cursor-pointer items-center gap-3 rounded-2xl border border-gray-200 p-3 transition-colors hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-700/50"
                  [class.border-blue-500]="selectedToolIds().has(tool.toolId)"
                  [class.bg-blue-50]="selectedToolIds().has(tool.toolId)"
                  [class.dark:border-blue-400]="selectedToolIds().has(tool.toolId)"
                  [class.dark:bg-blue-900/20]="selectedToolIds().has(tool.toolId)"
                >
                  <input
                    type="checkbox"
                    [checked]="selectedToolIds().has(tool.toolId)"
                    (change)="toggleTool(tool.toolId)"
                    class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700"
                  />
                  <div class="min-w-0 flex-1">
                    <div class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                      {{ tool.displayName }}
                    </div>
                    <div class="truncate font-mono text-xs/5 text-gray-500 dark:text-gray-400">
                      {{ tool.toolId }}
                    </div>
                  </div>
                  <span class="hidden shrink-0 rounded-2xl bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium capitalize text-gray-600 sm:inline-block dark:bg-gray-700 dark:text-gray-300">
                    {{ categoryLabel(tool.category) }}
                  </span>
                </label>
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

  categoryLabel(category: string): string {
    return TOOL_CATEGORIES.find((c) => c.value === category)?.label ?? category;
  }

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

  clearAll(): void {
    this.selectedToolIds.set(new Set());
  }

  confirm(): void {
    this.dialogRef.close(Array.from(this.selectedToolIds()));
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
