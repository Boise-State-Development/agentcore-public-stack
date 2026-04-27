import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroMagnifyingGlass,
  heroPencilSquare,
  heroTrash,
  heroUserGroup,
  heroXMark,
  heroGlobeAlt,
  heroCheck,
  heroXCircle,
  heroArrowLeft,
} from '@ng-icons/heroicons/outline';
import { AdminToolService } from '../services/admin-tool.service';
import { AdminTool, TOOL_CATEGORIES, TOOL_STATUSES } from '../models/admin-tool.model';
import { ToolRoleDialogComponent, ToolRoleDialogData, ToolRoleDialogResult } from '../components/tool-role-dialog.component';
import { DeleteToolDialogComponent, DeleteToolDialogData, DeleteToolDialogResult } from '../components/delete-tool-dialog.component';
import { TooltipDirective } from '../../../components/tooltip';

@Component({
  selector: 'app-tool-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroTrash,
      heroUserGroup,
      heroXMark,
      heroGlobeAlt,
      heroCheck,
      heroXCircle,
      heroArrowLeft,
    }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <!-- Back Button -->
    <a
      routerLink="/admin"
      class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
    >
      <ng-icon name="heroArrowLeft" class="size-4" />
      Back to Admin
    </a>

    <div class="mb-6 flex items-center justify-between">
      <div>
        <h1 class="text-3xl/9 font-bold">Tool Catalog</h1>
        <p class="text-gray-600 dark:text-gray-400">
          Manage tool metadata and role assignments.
        </p>
      </div>
      <div class="flex gap-2">
        <a
          routerLink="/admin/tools/new"
          class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:bg-blue-500 dark:hover:bg-blue-600"
        >
          <ng-icon name="heroPlus" class="size-5" />
          Add Tool
        </a>
      </div>
    </div>

    <!-- Filters -->
    <div class="mb-6 flex flex-wrap items-center gap-4">
      <div class="relative flex-1 min-w-64">
        <ng-icon
          name="heroMagnifyingGlass"
          class="absolute left-3 top-1/2 -translate-y-1/2 size-5 text-gray-400"
        />
        <input
          type="text"
          [(ngModel)]="searchQuery"
          placeholder="Search by name or ID..."
          class="w-full pl-10 pr-10 py-2 bg-white border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-500 dark:text-white dark:placeholder-gray-400"
        />
        @if (searchQuery()) {
          <button
            (click)="searchQuery.set('')"
            class="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
          >
            <ng-icon name="heroXMark" class="size-5" />
          </button>
        }
      </div>

      <select
        [ngModel]="statusFilter()"
        (ngModelChange)="statusFilter.set($event)"
        class="px-3 py-2 bg-white border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-500 dark:text-white"
      >
        <option value="">All Statuses</option>
        @for (status of statuses; track status.value) {
          <option [value]="status.value">{{ status.label }}</option>
        }
      </select>

      <select
        [ngModel]="categoryFilter()"
        (ngModelChange)="categoryFilter.set($event)"
        class="px-3 py-2 bg-white border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-500 dark:text-white"
      >
        <option value="">All Categories</option>
        @for (cat of categories; track cat.value) {
          <option [value]="cat.value">{{ cat.label }}</option>
        }
      </select>

      @if (hasActiveFilters()) {
        <button
          (click)="resetFilters()"
          class="text-sm text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
        >
          Clear Filters
        </button>
      }
    </div>

    <!-- Loading State -->
    @if (toolsResource.isLoading() && tools().length === 0) {
      <div class="flex items-center justify-center h-64">
        <div class="flex flex-col items-center gap-4">
          <div
            class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"
          ></div>
          <p class="text-sm text-gray-500 dark:text-gray-400">
            Loading tools...
          </p>
        </div>
      </div>
    }

    <!-- Error State -->
    @if (toolsResource.error()) {
      <div class="mb-6 p-4 bg-red-50 border border-red-200 rounded-sm text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-200">
        <p>Failed to load tools. Please try again.</p>
        <button
          (click)="adminToolService.reload()"
          class="mt-2 text-sm underline hover:no-underline"
        >
          Retry
        </button>
      </div>
    }

    <!-- Tools Table -->
    @if (!toolsResource.isLoading() || tools().length > 0) {
      <div class="bg-white dark:bg-gray-800 rounded-sm shadow-xs overflow-hidden border border-gray-200 dark:border-gray-700">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
          <thead class="bg-gray-50 dark:bg-gray-700">
            <tr>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                Tool
              </th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                Category
              </th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                Access
              </th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                Default
              </th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                Status
              </th>
              <th class="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
            @for (tool of filteredTools(); track tool.toolId) {
              <tr class="hover:bg-gray-50 dark:hover:bg-gray-700">
                <td class="px-6 py-4">
                  <div>
                    <div class="font-medium">{{ tool.displayName }}</div>
                    <div class="text-sm text-gray-500 dark:text-gray-400">{{ tool.toolId }}</div>
                  </div>
                </td>
                <td class="px-6 py-4">
                  <span class="px-2 py-1 text-xs rounded-xs bg-gray-100 dark:bg-gray-600 capitalize">
                    {{ tool.category }}
                  </span>
                </td>
                <td class="px-6 py-4">
                  @if (tool.isPublic) {
                    <span class="inline-flex items-center gap-1 text-green-600 dark:text-green-400">
                      <ng-icon name="heroGlobeAlt" class="size-4" />
                      Public
                    </span>
                  } @else {
                    <span class="text-gray-600 dark:text-gray-400">
                      {{ tool.allowedAppRoles.length }} roles
                    </span>
                  }
                </td>
                <td class="px-6 py-4">
                  @if (tool.enabledByDefault) {
                    <ng-icon name="heroCheck" class="size-5 text-green-600 dark:text-green-400" />
                  } @else {
                    <ng-icon name="heroXCircle" class="size-5 text-gray-400" />
                  }
                </td>
                <td class="px-6 py-4">
                  <span [class]="getStatusClass(tool.status)">
                    {{ tool.status }}
                  </span>
                </td>
                <td class="px-6 py-4 text-right">
                  <div class="flex items-center justify-end gap-1">
                    <button
                      (click)="openRoleDialog(tool)"
                      class="p-2 text-gray-500 hover:text-blue-600 hover:bg-gray-100 rounded-sm dark:hover:bg-gray-600"
                      [appTooltip]="'Manage Role Access'"
                      appTooltipPosition="top"
                    >
                      <ng-icon name="heroUserGroup" class="size-5" />
                    </button>
                    <a
                      [routerLink]="['/admin/tools/edit', tool.toolId]"
                      class="p-2 text-gray-500 hover:text-blue-600 hover:bg-gray-100 rounded-sm dark:hover:bg-gray-600"
                      [appTooltip]="'Edit Tool'"
                      appTooltipPosition="top"
                    >
                      <ng-icon name="heroPencilSquare" class="size-5" />
                    </a>
                    <button
                      (click)="deleteTool(tool)"
                      class="p-2 text-gray-500 hover:text-red-600 hover:bg-gray-100 rounded-sm dark:hover:bg-gray-600"
                      [appTooltip]="'Delete Tool'"
                      appTooltipPosition="top"
                    >
                      <ng-icon name="heroTrash" class="size-5" />
                    </button>
                  </div>
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>

      <!-- Empty State -->
      @if (filteredTools().length === 0 && !toolsResource.isLoading()) {
        <div class="text-center py-12 text-gray-500">
          <ng-icon name="heroPlus" class="size-12 mx-auto mb-4 text-gray-300" />
          @if (hasActiveFilters()) {
            <p class="text-lg/7">No tools match your filters</p>
            <p class="text-sm/6">Try adjusting your search or filter criteria</p>
          } @else {
            <p class="text-lg/7">No tools in catalog</p>
            <p class="text-sm/6 mb-4">Add a tool to get started.</p>
            <a
              routerLink="/admin/tools/new"
              class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700"
            >
              <ng-icon name="heroPlus" class="size-5" />
              Add Tool
            </a>
          }
        </div>
      }
    }
  `,
})
export class ToolListPage {
  adminToolService = inject(AdminToolService);
  private router = inject(Router);
  private dialog = inject(Dialog);

  readonly toolsResource = this.adminToolService.toolsResource;
  readonly categories = TOOL_CATEGORIES;
  readonly statuses = TOOL_STATUSES;

  // Local state
  searchQuery = signal('');
  statusFilter = signal('');
  categoryFilter = signal('');

  // Computed
  readonly tools = computed(() => this.adminToolService.getTools());

  readonly filteredTools = computed(() => {
    let tools = this.tools();
    const query = this.searchQuery().toLowerCase();
    const status = this.statusFilter();
    const category = this.categoryFilter();

    if (query) {
      tools = tools.filter(
        t =>
          t.displayName.toLowerCase().includes(query) ||
          t.toolId.toLowerCase().includes(query) ||
          t.description.toLowerCase().includes(query)
      );
    }

    if (status) {
      tools = tools.filter(t => t.status === status);
    }

    if (category) {
      tools = tools.filter(t => t.category === category);
    }

    return tools.sort((a, b) => {
      // Sort by category, then by display name
      const catCompare = a.category.localeCompare(b.category);
      if (catCompare !== 0) return catCompare;
      return a.displayName.localeCompare(b.displayName);
    });
  });

  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.statusFilter() || this.categoryFilter());
  });

  resetFilters(): void {
    this.searchQuery.set('');
    this.statusFilter.set('');
    this.categoryFilter.set('');
  }

  getStatusClass(status: string): string {
    switch (status) {
      case 'active':
        return 'px-2 py-1 text-xs rounded-xs bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300';
      case 'deprecated':
        return 'px-2 py-1 text-xs rounded-xs bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300';
      case 'disabled':
        return 'px-2 py-1 text-xs rounded-xs bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300';
      case 'coming_soon':
        return 'px-2 py-1 text-xs rounded-xs bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300';
      default:
        return 'px-2 py-1 text-xs rounded-xs bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
    }
  }

  async openRoleDialog(tool: AdminTool): Promise<void> {
    const dialogRef = this.dialog.open<ToolRoleDialogResult>(ToolRoleDialogComponent, {
      data: { tool } as ToolRoleDialogData,
    });

    const result = await firstValueFrom(dialogRef.closed);
    if (result !== undefined) {
      try {
        await this.adminToolService.setToolRoles(tool.toolId, result);
      } catch (error: unknown) {
        console.error('Error saving roles:', error);
        const message = error instanceof Error ? error.message : 'Failed to save roles.';
        alert(message);
      }
    }
  }

  async deleteTool(tool: AdminTool): Promise<void> {
    const dialogRef = this.dialog.open<DeleteToolDialogResult>(DeleteToolDialogComponent, {
      data: {
        toolId: tool.toolId,
        displayName: tool.displayName,
      } as DeleteToolDialogData,
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed) {
      try {
        await this.adminToolService.deleteTool(tool.toolId, true);
      } catch (error: unknown) {
        console.error('Error deleting tool:', error);
        const message = error instanceof Error ? error.message : 'Failed to delete tool.';
        alert(message);
      }
    }
  }
}
