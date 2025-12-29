import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroMagnifyingGlass,
  heroPencilSquare,
  heroTrash,
  heroShieldCheck,
  heroXMark,
  heroArrowPath,
  heroChevronRight,
} from '@ng-icons/heroicons/outline';
import { AppRolesService } from '../services/app-roles.service';
import { AppRole } from '../models/app-role.model';
import { ToolsService } from '../../tools/services/tools.service';

@Component({
  selector: 'app-role-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroTrash,
      heroShieldCheck,
      heroXMark,
      heroArrowPath,
      heroChevronRight,
    }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <div class="mb-6 flex items-center justify-between">
      <div>
        <h1 class="text-2xl/9 font-bold">Role Management</h1>
        <p class="text-gray-600 dark:text-gray-400">
          Manage application roles, permissions, and JWT mappings.
        </p>
      </div>
      <a
        routerLink="/admin/roles/new"
        class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:bg-blue-500 dark:hover:bg-blue-600"
      >
        <ng-icon name="heroPlus" class="size-5" />
        Create Role
      </a>
    </div>

    <!-- Search and Filters -->
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
          class="w-full pl-10 pr-10 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
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
        [ngModel]="enabledFilter()"
        (ngModelChange)="enabledFilter.set($event)"
        class="px-3 py-2 border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-600"
      >
        <option value="">All Roles</option>
        <option value="enabled">Enabled Only</option>
        <option value="disabled">Disabled Only</option>
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
    @if (rolesResource.isLoading() && roles().length === 0) {
      <div class="flex items-center justify-center h-64">
        <div class="flex flex-col items-center gap-4">
          <div
            class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"
          ></div>
          <p class="text-sm text-gray-500 dark:text-gray-400">
            Loading roles...
          </p>
        </div>
      </div>
    }

    <!-- Error State -->
    @if (rolesResource.error()) {
      <div class="mb-6 p-4 bg-red-50 border border-red-200 rounded-sm text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-200">
        <p>Failed to load roles. Please try again.</p>
        <button
          (click)="appRolesService.reload()"
          class="mt-2 text-sm underline hover:no-underline"
        >
          Retry
        </button>
      </div>
    }

    <!-- Roles List -->
    @if (!rolesResource.isLoading() || roles().length > 0) {
      <div class="space-y-3">
        @for (role of filteredRoles(); track role.roleId) {
          <div
            class="border border-gray-200 rounded-sm bg-white dark:border-gray-700 dark:bg-gray-800"
          >
            <div class="p-4">
              <div class="flex items-start justify-between gap-4">
                <!-- Role Info -->
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2 mb-1">
                    <span class="font-medium text-lg/7">{{ role.displayName }}</span>
                    @if (role.isSystemRole) {
                      <span
                        class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-xs bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300"
                        title="System roles cannot be deleted"
                      >
                        <ng-icon name="heroShieldCheck" class="size-3" />
                        System
                      </span>
                    }
                    @if (!role.enabled) {
                      <span class="px-2 py-0.5 text-xs font-medium rounded-xs bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                        Disabled
                      </span>
                    }
                  </div>
                  <p class="text-sm text-gray-500 dark:text-gray-400 mb-2">
                    {{ role.roleId }}
                  </p>
                  @if (role.description) {
                    <p class="text-sm/6 text-gray-600 dark:text-gray-300 mb-3">
                      {{ role.description }}
                    </p>
                  }

                  <!-- Role Details Grid -->
                  <div class="grid grid-cols-1 gap-3 sm:grid-cols-3 text-sm">
                    <!-- JWT Mappings -->
                    <div>
                      <span class="font-medium text-gray-700 dark:text-gray-300">JWT Roles:</span>
                      <div class="mt-1 flex flex-wrap gap-1">
                        @if (role.jwtRoleMappings.length > 0) {
                          @for (jwt of role.jwtRoleMappings.slice(0, 3); track jwt) {
                            <span class="px-1.5 py-0.5 text-xs rounded-xs bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300">
                              {{ jwt }}
                            </span>
                          }
                          @if (role.jwtRoleMappings.length > 3) {
                            <span class="px-1.5 py-0.5 text-xs rounded-xs bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                              +{{ role.jwtRoleMappings.length - 3 }} more
                            </span>
                          }
                        } @else {
                          <span class="text-gray-400 dark:text-gray-500">None</span>
                        }
                      </div>
                    </div>

                    <!-- Tools -->
                    <div>
                      <span class="font-medium text-gray-700 dark:text-gray-300">Tools:</span>
                      <div class="mt-1 flex flex-wrap gap-1">
                        @if (role.grantedTools.length > 0) {
                          @if (role.grantedTools.includes('*')) {
                            <span class="px-1.5 py-0.5 text-xs rounded-xs bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300">
                              All Tools
                            </span>
                          } @else {
                            @for (tool of role.grantedTools.slice(0, 2); track tool) {
                              <span class="px-1.5 py-0.5 text-xs rounded-xs bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300">
                                {{ getToolDisplayName(tool) }}
                              </span>
                            }
                            @if (role.grantedTools.length > 2) {
                              <span class="px-1.5 py-0.5 text-xs rounded-xs bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                                +{{ role.grantedTools.length - 2 }} more
                              </span>
                            }
                          }
                        } @else {
                          <span class="text-gray-400 dark:text-gray-500">None</span>
                        }
                      </div>
                    </div>

                    <!-- Priority -->
                    <div>
                      <span class="font-medium text-gray-700 dark:text-gray-300">Priority:</span>
                      <div class="mt-1">
                        <span class="text-gray-600 dark:text-gray-400">{{ role.priority }}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Actions -->
                <div class="flex items-center gap-2 shrink-0">
                  <a
                    [routerLink]="['/admin/roles/edit', role.roleId]"
                    class="p-2 text-gray-500 hover:text-blue-600 hover:bg-gray-100 rounded-sm dark:hover:bg-gray-700 dark:hover:text-blue-400"
                    title="Edit role"
                  >
                    <ng-icon name="heroPencilSquare" class="size-5" />
                  </a>
                  @if (!role.isSystemRole) {
                    <button
                      (click)="deleteRole(role)"
                      class="p-2 text-gray-500 hover:text-red-600 hover:bg-gray-100 rounded-sm dark:hover:bg-gray-700 dark:hover:text-red-400"
                      title="Delete role"
                    >
                      <ng-icon name="heroTrash" class="size-5" />
                    </button>
                  }
                  <button
                    (click)="syncPermissions(role)"
                    [disabled]="syncing() === role.roleId"
                    class="p-2 text-gray-500 hover:text-green-600 hover:bg-gray-100 rounded-sm dark:hover:bg-gray-700 dark:hover:text-green-400 disabled:opacity-50"
                    title="Sync permissions"
                  >
                    <ng-icon
                      name="heroArrowPath"
                      class="size-5"
                      [class.animate-spin]="syncing() === role.roleId"
                    />
                  </button>
                </div>
              </div>
            </div>
          </div>
        }
      </div>

      <!-- Empty State -->
      @if (filteredRoles().length === 0 && !rolesResource.isLoading()) {
        <div class="text-center py-12 text-gray-500">
          <ng-icon name="heroShieldCheck" class="size-12 mx-auto mb-4 text-gray-300" />
          @if (hasActiveFilters()) {
            <p class="text-lg/7">No roles match your filters</p>
            <p class="text-sm/6">Try adjusting your search or filter criteria</p>
          } @else {
            <p class="text-lg/7">No roles configured</p>
            <p class="text-sm/6 mb-4">Create your first application role to get started</p>
            <a
              routerLink="/admin/roles/new"
              class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700"
            >
              <ng-icon name="heroPlus" class="size-5" />
              Create Role
            </a>
          }
        </div>
      }
    }
  `,
})
export class RoleListPage {
  appRolesService = inject(AppRolesService);
  private toolsService = inject(ToolsService);
  private router = inject(Router);

  readonly rolesResource = this.appRolesService.rolesResource;

  // Local state
  searchQuery = signal('');
  enabledFilter = signal('');
  syncing = signal<string | null>(null);

  // Computed
  readonly roles = computed(() => this.appRolesService.getRoles());

  readonly filteredRoles = computed(() => {
    let roles = this.roles();
    const query = this.searchQuery().toLowerCase();
    const enabled = this.enabledFilter();

    if (query) {
      roles = roles.filter(
        r =>
          r.displayName.toLowerCase().includes(query) ||
          r.roleId.toLowerCase().includes(query) ||
          r.description.toLowerCase().includes(query)
      );
    }

    if (enabled === 'enabled') {
      roles = roles.filter(r => r.enabled);
    } else if (enabled === 'disabled') {
      roles = roles.filter(r => !r.enabled);
    }

    // Sort: enabled first, then by priority (desc), then by name
    return roles.sort((a, b) => {
      if (a.enabled !== b.enabled) {
        return a.enabled ? -1 : 1;
      }
      if (a.priority !== b.priority) {
        return b.priority - a.priority;
      }
      return a.displayName.localeCompare(b.displayName);
    });
  });

  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.enabledFilter());
  });

  resetFilters(): void {
    this.searchQuery.set('');
    this.enabledFilter.set('');
  }

  getToolDisplayName(toolId: string): string {
    const tool = this.toolsService.getToolById(toolId);
    return tool?.name ?? toolId;
  }

  async deleteRole(role: AppRole): Promise<void> {
    if (role.isSystemRole) {
      alert('System roles cannot be deleted.');
      return;
    }

    if (!confirm(`Are you sure you want to delete the role "${role.displayName}"? This action cannot be undone.`)) {
      return;
    }

    try {
      await this.appRolesService.deleteRole(role.roleId);
    } catch (error: any) {
      console.error('Error deleting role:', error);
      const message = error?.error?.detail || error?.message || 'Failed to delete role.';
      alert(message);
    }
  }

  async syncPermissions(role: AppRole): Promise<void> {
    this.syncing.set(role.roleId);
    try {
      await this.appRolesService.syncPermissions(role.roleId);
    } catch (error: any) {
      console.error('Error syncing permissions:', error);
      const message = error?.error?.detail || error?.message || 'Failed to sync permissions.';
      alert(message);
    } finally {
      this.syncing.set(null);
    }
  }
}
