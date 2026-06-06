import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  effect,
  DestroyRef,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroMagnifyingGlass,
  heroChevronDown,
  heroPencilSquare,
  heroTrash,
  heroUserGroup,
  heroGlobeAlt,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { heroStarSolid } from '@ng-icons/heroicons/solid';
import { AdminToolService } from '../services/admin-tool.service';
import {
  AdminTool,
  GatewayTargetStatus,
  TOOL_CATEGORIES,
  TOOL_STATUSES,
  TOOL_PROTOCOLS,
} from '../models/admin-tool.model';

/** A gateway health value the row can render, including transient UI states. */
export type GatewayHealth = GatewayTargetStatus | 'loading' | 'error';

/** Compact badge descriptor derived from a tool's gateway health. */
export interface GatewayBadge {
  label: string;
  cls: string;
  title: string;
  failed: boolean;
}

const GATEWAY_BADGE_BASE =
  'shrink-0 inline-flex items-center gap-1 rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';

/** Gateway target statuses that are still settling (not yet Ready/Failed). */
const TRANSIENT_GATEWAY_STATUSES = ['CREATING', 'UPDATING', 'SYNCHRONIZING'];

/** Map a tool's gateway health to a compact badge, or null if not yet known. */
export function gatewayBadgeFor(health: GatewayHealth | undefined): GatewayBadge | null {
  if (health === undefined) {
    return null;
  }
  const muted = `${GATEWAY_BADGE_BASE} bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400`;
  if (health === 'loading') {
    return { label: 'Checking…', cls: muted, title: 'Checking gateway target health', failed: false };
  }
  if (health === 'error') {
    return { label: 'Unknown', cls: muted, title: 'Could not fetch gateway target health', failed: false };
  }
  if (health.healthy) {
    return {
      label: 'Ready',
      cls: `${GATEWAY_BADGE_BASE} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`,
      title: 'Gateway target is ready',
      failed: false,
    };
  }
  if (TRANSIENT_GATEWAY_STATUSES.includes(health.status.toUpperCase())) {
    return {
      label: 'Syncing',
      cls: `${GATEWAY_BADGE_BASE} bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300`,
      title: 'The gateway is connecting to the target and listing its tools…',
      failed: false,
    };
  }
  return {
    label: health.status.toUpperCase() === 'MISSING' ? 'Missing' : 'Failed',
    cls: `${GATEWAY_BADGE_BASE} bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300`,
    title: health.statusReasons.join(' ') || 'Gateway target is not usable',
    failed: true,
  };
}

/** The joined failure reasons for an unhealthy gateway health, else null. */
export function gatewayFailureReasonsFor(health: GatewayHealth | undefined): string | null {
  if (!health || health === 'loading' || health === 'error' || health.healthy) {
    return null;
  }
  return health.statusReasons.join(' ') || null;
}

/** Whether a gateway status string is still settling (drives re-polling). */
export function isTransientGatewayStatus(status: string): boolean {
  return TRANSIENT_GATEWAY_STATUSES.includes(status.toUpperCase());
}
import { AppRolesService } from '../../roles/services/app-roles.service';
import { ToolRoleDialogComponent, ToolRoleDialogData, ToolRoleDialogResult } from '../components/tool-role-dialog.component';
import { DeleteToolDialogComponent, DeleteToolDialogData, DeleteToolDialogResult } from '../components/delete-tool-dialog.component';

@Component({
  selector: 'app-tool-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroChevronDown,
      heroPencilSquare,
      heroTrash,
      heroUserGroup,
      heroGlobeAlt,
      heroExclamationTriangle,
      heroStarSolid,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Page Header -->
        <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Tool Catalog</h1>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Manage tool metadata and role assignments.
            </p>
          </div>
          <a
            routerLink="/admin/tools/new"
            class="inline-flex shrink-0 items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
            Add Tool
          </a>
        </div>

        <!-- Toolbar: search + filters inline -->
        <div class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
          <div class="relative flex-1">
            <ng-icon
              name="heroMagnifyingGlass"
              class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <label for="search" class="sr-only">Search tools</label>
            <input
              type="text"
              id="search"
              [ngModel]="searchQuery()"
              (ngModelChange)="searchQuery.set($event)"
              placeholder="Search by name, ID, or description…"
              class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            />
          </div>

          <label for="status" class="sr-only">Filter by status</label>
          <select
            id="status"
            [ngModel]="statusFilter()"
            (ngModelChange)="statusFilter.set($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All statuses</option>
            @for (status of statuses; track status.value) {
              <option [value]="status.value">{{ status.label }}</option>
            }
          </select>

          <label for="category" class="sr-only">Filter by category</label>
          <select
            id="category"
            [ngModel]="categoryFilter()"
            (ngModelChange)="categoryFilter.set($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All categories</option>
            @for (cat of categories; track cat.value) {
              <option [value]="cat.value">{{ cat.label }}</option>
            }
          </select>

          @if (hasActiveFilters()) {
            <button
              (click)="resetFilters()"
              class="rounded-2xl px-3 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
            >
              Reset
            </button>
          }
        </div>

        <!-- Count -->
        <div class="mb-3 text-xs/5 text-gray-500 dark:text-gray-400">
          {{ filteredTools().length }} tool{{ filteredTools().length !== 1 ? 's' : '' }}
        </div>

        <!-- Loading State -->
        @if (toolsResource.isLoading() && tools().length === 0) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div
                class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
              ></div>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading tools…</p>
            </div>
          </div>
        }

        <!-- Error State -->
        @if (toolsResource.error()) {
          <div class="mb-6 rounded-2xl border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200">
            <p class="text-sm/6">Failed to load tools. Please try again.</p>
            <button
              (click)="adminToolService.reload()"
              class="mt-2 text-sm/6 font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        }

        <!-- Tools List -->
        @if (!toolsResource.isLoading() || tools().length > 0) {
          @if (filteredTools().length === 0) {
            <div class="rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
              @if (hasActiveFilters()) {
                <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                  No tools match the current filters.
                </p>
              } @else {
                <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                  No tools in catalog yet.
                </p>
                <a
                  routerLink="/admin/tools/new"
                  class="mt-4 inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
                  Add Tool
                </a>
              }
            </div>
          } @else {
            <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
              @for (tool of filteredTools(); track tool.toolId) {
                <li>
                  <!-- Row -->
                  <div class="flex items-center gap-3 px-3 py-2.5 sm:px-4">
                    <!-- Expand toggle -->
                    <button
                      type="button"
                      (click)="toggleExpand(tool.toolId)"
                      [attr.aria-expanded]="isExpanded(tool.toolId)"
                      [attr.aria-controls]="'tool-detail-' + tool.toolId"
                      [attr.aria-label]="(isExpanded(tool.toolId) ? 'Hide' : 'Show') + ' details for ' + tool.displayName"
                      class="flex size-7 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                    >
                      <ng-icon
                        name="heroChevronDown"
                        class="size-4 transition-transform duration-150"
                        [class.rotate-180]="isExpanded(tool.toolId)"
                        aria-hidden="true"
                      />
                    </button>

                    <!-- Name + tool id -->
                    <div class="min-w-0 flex-1">
                      <div class="flex items-center gap-1.5">
                        <span class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                          {{ tool.displayName }}
                        </span>
                        @if (tool.enabledByDefault) {
                          <ng-icon
                            name="heroStarSolid"
                            class="size-4 shrink-0 text-amber-500 dark:text-amber-400"
                            aria-label="Enabled by default"
                          />
                        }
                      </div>
                      <p class="truncate font-mono text-xs/5 text-gray-500 dark:text-gray-400">
                        {{ tool.toolId }}
                      </p>
                    </div>

                    <!-- Category -->
                    <span class="hidden shrink-0 rounded-2xl bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium capitalize text-gray-600 sm:inline-block dark:bg-gray-700 dark:text-gray-300">
                      {{ getCategoryLabel(tool.category) }}
                    </span>

                    <!-- Access -->
                    <span class="hidden w-20 shrink-0 justify-end text-right text-xs/5 sm:flex">
                      @if (tool.isPublic) {
                        <span class="inline-flex items-center gap-1 font-medium text-green-700 dark:text-green-400">
                          <ng-icon name="heroGlobeAlt" class="size-4" aria-hidden="true" />
                          Public
                        </span>
                      } @else {
                        <span class="text-gray-500 dark:text-gray-400">
                          {{ tool.allowedAppRoles.length }} role{{ tool.allowedAppRoles.length !== 1 ? 's' : '' }}
                        </span>
                      }
                    </span>

                    <!-- Gateway target health (protocol=mcp only) -->
                    @if (tool.protocol === 'mcp' && gatewayBadge(tool.toolId); as badge) {
                      <span [class]="badge.cls" [title]="badge.title">
                        @if (badge.failed) {
                          <ng-icon name="heroExclamationTriangle" class="size-3.5" aria-hidden="true" />
                        }
                        {{ badge.label }}
                      </span>
                    }

                    <!-- Status -->
                    <span [class]="getStatusClass(tool.status)">
                      {{ tool.status }}
                    </span>

                    <!-- Actions -->
                    <div class="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        (click)="openRoleDialog(tool)"
                        [attr.aria-label]="'Manage role access for ' + tool.displayName"
                        [title]="'Manage role access for ' + tool.displayName"
                        class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon name="heroUserGroup" class="size-4" aria-hidden="true" />
                      </button>
                      <a
                        [routerLink]="['/admin/tools/edit', tool.toolId]"
                        [attr.aria-label]="'Edit ' + tool.displayName"
                        [title]="'Edit ' + tool.displayName"
                        class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
                      </a>
                      <button
                        type="button"
                        (click)="deleteTool(tool)"
                        [attr.aria-label]="'Delete ' + tool.displayName"
                        [title]="'Delete ' + tool.displayName"
                        class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                      >
                        <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                      </button>
                    </div>
                  </div>

                  <!-- Expanded detail -->
                  @if (isExpanded(tool.toolId)) {
                    <div
                      [id]="'tool-detail-' + tool.toolId"
                      class="border-t border-gray-100 bg-gray-50 px-4 py-3 sm:pl-14 dark:border-gray-700/60 dark:bg-gray-900/40"
                    >
                      <dl class="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-3">
                        <div class="sm:col-span-3">
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            Description
                          </dt>
                          <dd class="mt-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                            {{ tool.description || 'No description provided.' }}
                          </dd>
                        </div>

                        <div>
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            Protocol
                          </dt>
                          <dd class="mt-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                            {{ getProtocolLabel(tool.protocol) }}
                          </dd>
                        </div>

                        <div>
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            Default
                          </dt>
                          <dd class="mt-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                            {{ tool.enabledByDefault ? 'On by default' : 'Off by default' }}
                          </dd>
                        </div>

                        <div>
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            OAuth
                          </dt>
                          <dd class="mt-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                            {{ tool.requiresOauthProvider || 'None' }}
                            @if (tool.forwardAuthToken) {
                              <span class="text-gray-400 dark:text-gray-500">· forwards auth token</span>
                            }
                          </dd>
                        </div>

                        <div class="sm:col-span-3">
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            Access
                          </dt>
                          @if (tool.isPublic) {
                            <dd class="mt-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                              Public — available to all authenticated users.
                            </dd>
                          } @else {
                            <dd class="mt-1 flex flex-wrap gap-1.5">
                              @if (tool.allowedAppRoles.length > 0) {
                                @for (roleId of tool.allowedAppRoles; track roleId) {
                                  <span
                                    class="inline-flex items-center rounded-2xl bg-purple-100 px-2 py-0.5 text-xs/5 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300"
                                    [title]="roleId"
                                  >
                                    {{ getRoleDisplayName(roleId) }}
                                  </span>
                                }
                              } @else {
                                <span class="text-xs/5 italic text-gray-500 dark:text-gray-400">No roles assigned</span>
                              }
                            </dd>
                          }
                        </div>

                        @if (tool.mcpConfig) {
                          <div class="sm:col-span-3">
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              MCP server
                            </dt>
                            <dd class="mt-0.5 space-y-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                              <p class="break-all font-mono text-xs/5">{{ tool.mcpConfig.serverUrl }}</p>
                              <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                                {{ tool.mcpConfig.transport }} · auth: {{ tool.mcpConfig.authType }} ·
                                {{ tool.mcpConfig.tools.length }} tool{{ tool.mcpConfig.tools.length !== 1 ? 's' : '' }}
                              </p>
                            </dd>
                          </div>
                        }

                        @if (tool.a2aConfig) {
                          <div class="sm:col-span-3">
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              A2A agent
                            </dt>
                            <dd class="mt-0.5 space-y-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                              <p class="break-all font-mono text-xs/5">{{ tool.a2aConfig.agentUrl }}</p>
                              <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                                auth: {{ tool.a2aConfig.authType }}
                                @if (tool.a2aConfig.capabilities.length > 0) {
                                  · {{ tool.a2aConfig.capabilities.join(', ') }}
                                }
                              </p>
                            </dd>
                          </div>
                        }

                        @if (tool.mcpGatewayConfig) {
                          <div class="sm:col-span-3">
                            <dt class="flex items-center gap-2 text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Gateway target
                              @if (gatewayBadge(tool.toolId); as badge) {
                                <span [class]="badge.cls" [title]="badge.title">
                                  @if (badge.failed) {
                                    <ng-icon name="heroExclamationTriangle" class="size-3.5" aria-hidden="true" />
                                  }
                                  {{ badge.label }}
                                </span>
                              }
                            </dt>
                            <dd class="mt-0.5 space-y-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                              <p class="break-all font-mono text-xs/5">{{ tool.mcpGatewayConfig.endpointUrl }}</p>
                              <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                                target: {{ tool.mcpGatewayConfig.targetName }} ·
                                outbound: {{ tool.mcpGatewayConfig.credentialType }} ·
                                {{ tool.mcpGatewayConfig.tools.length }} tool{{ tool.mcpGatewayConfig.tools.length !== 1 ? 's' : '' }}
                              </p>
                              @if (gatewayFailureReasons(tool.toolId); as reasons) {
                                <p class="mt-1 rounded-xl bg-red-50 px-2.5 py-1.5 text-xs/5 text-red-800 dark:bg-red-900/20 dark:text-red-200">
                                  {{ reasons }}
                                </p>
                              }
                            </dd>
                          </div>
                        }
                      </dl>
                    </div>
                  }
                </li>
              }
            </ul>
          }
        }
      </div>
    </div>
  `,
})
export class ToolListPage {
  adminToolService = inject(AdminToolService);
  private dialog = inject(Dialog);
  private appRolesService = inject(AppRolesService);

  readonly toolsResource = this.adminToolService.toolsResource;
  readonly categories = TOOL_CATEGORIES;
  readonly statuses = TOOL_STATUSES;

  // Local state
  searchQuery = signal('');
  statusFilter = signal('');
  categoryFilter = signal('');

  // Row detail expansion state (set of tool ids currently expanded)
  private expandedIds = signal<ReadonlySet<string>>(new Set());

  // Live gateway-target health per protocol='mcp' tool id. Lazily populated so
  // a FAILED target (e.g. the gateway role can't invoke the endpoint) surfaces
  // as a badge instead of only appearing later as "the agent can't see it".
  private gatewayStatuses = signal<ReadonlyMap<string, GatewayHealth>>(new Map());
  // Dedupe guard (plain field, not a signal, so the fetch effect doesn't depend
  // on it and re-trigger itself).
  private requestedStatusToolIds = new Set<string>();
  private destroyRef = inject(DestroyRef);
  private destroyed = false;

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.destroyed = true;
    });
    // Fetch live gateway health for each protocol='mcp' tool once, as the
    // catalog loads. Typically a handful of rows, so per-row lazy fetch is fine.
    effect(() => {
      for (const tool of this.tools()) {
        if (tool.protocol === 'mcp' && !this.requestedStatusToolIds.has(tool.toolId)) {
          this.requestedStatusToolIds.add(tool.toolId);
          void this.loadGatewayStatus(tool.toolId);
        }
      }
    });
  }

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

  isExpanded(toolId: string): boolean {
    return this.expandedIds().has(toolId);
  }

  toggleExpand(toolId: string): void {
    this.expandedIds.update(current => {
      const next = new Set(current);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
  }

  getCategoryLabel(category: string): string {
    return this.categories.find(c => c.value === category)?.label ?? category;
  }

  getProtocolLabel(protocol: string): string {
    return TOOL_PROTOCOLS.find(p => p.value === protocol)?.label ?? protocol;
  }

  /**
   * Get the display name for a role ID.
   * Falls back to the role ID if not found.
   */
  getRoleDisplayName(roleId: string): string {
    const role = this.appRolesService.getRoleById(roleId);
    return role?.displayName ?? roleId;
  }

  getStatusClass(status: string): string {
    const base =
      'shrink-0 rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
    switch (status) {
      case 'active':
        return `${base} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`;
      case 'deprecated':
        return `${base} bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300`;
      case 'disabled':
        return `${base} bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300`;
      case 'coming_soon':
        return `${base} bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300`;
      default:
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  }

  private async loadGatewayStatus(toolId: string, attempt = 0): Promise<void> {
    if (attempt === 0) {
      this.setGatewayStatus(toolId, 'loading');
    }
    try {
      const status = await this.adminToolService.getGatewayTargetStatus(toolId);
      if (this.destroyed) {
        return;
      }
      this.setGatewayStatus(toolId, status);
      // The gateway syncs a target asynchronously, so a freshly-saved tool is
      // briefly transient. Re-poll a few times until it settles into
      // Ready/Failed, so the badge converges without a manual reload.
      if (isTransientGatewayStatus(status.status) && attempt < 5) {
        setTimeout(() => void this.loadGatewayStatus(toolId, attempt + 1), 3000);
      }
    } catch {
      // Keep any prior value on a re-poll failure; only the first attempt
      // surfaces an explicit 'unknown'.
      if (attempt === 0 && !this.destroyed) {
        this.setGatewayStatus(toolId, 'error');
      }
    }
  }

  private setGatewayStatus(toolId: string, value: GatewayHealth): void {
    this.gatewayStatuses.update(prev => {
      const next = new Map(prev);
      next.set(toolId, value);
      return next;
    });
  }

  /** Compact health badge for a gateway tool's row, or null if not yet known. */
  gatewayBadge(toolId: string): GatewayBadge | null {
    return gatewayBadgeFor(this.gatewayStatuses().get(toolId));
  }

  /** The joined failure reasons for an unhealthy gateway tool, else null. */
  gatewayFailureReasons(toolId: string): string | null {
    return gatewayFailureReasonsFor(this.gatewayStatuses().get(toolId));
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
