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
  heroXMark,
  heroArrowLeft,
  heroLink,
  heroCloud,
  heroCodeBracket,
  heroAcademicCap,
  heroCheck,
  heroXCircle,
  heroShieldCheck,
} from '@ng-icons/heroicons/outline';
import { ConnectorsService } from '../services/connectors.service';
import { Connector, ConnectorType } from '../models/connector.model';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

@Component({
  selector: 'app-connector-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon, TooltipDirective, SpinnerComponent],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroTrash,
      heroXMark,
      heroArrowLeft,
      heroLink,
      heroCloud,
      heroCodeBracket,
      heroAcademicCap,
      heroCheck,
      heroXCircle,
      heroShieldCheck,
    }),
  ],
  host: {
    class: 'block',
  },
  template: `
    <div>
        <!-- Page Header -->
        <div class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Connectors</h1>
            <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
              Configure third-party OAuth integrations that users can connect for MCP tool authentication.
            </p>
          </div>
          <a
            routerLink="/admin/connectors/new"
            class="inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-sm bg-primary-accessible px-4 py-2.5 text-sm/6 font-semibold text-white shadow-xs hover:brightness-95 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50"
          >
            <ng-icon name="heroPlus" class="size-5" />
            Add Connector
          </a>
        </div>

        <!-- Search and Filters -->
        <div class="mb-6 flex flex-wrap items-center gap-4">
          <div class="relative min-w-64 flex-1">
            <ng-icon
              name="heroMagnifyingGlass"
              class="absolute left-3 top-1/2 size-5 -translate-y-1/2 text-gray-400"
            />
            <input
              type="text"
              [(ngModel)]="searchQuery"
              placeholder="Search connectors..."
              class="w-full rounded-sm border border-gray-300 bg-white py-2.5 pl-10 pr-10 text-sm/6 placeholder:text-gray-400 focus:border-primary-500 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            />
            @if (searchQuery()) {
              <button
                (click)="searchQuery.set('')"
                class="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                [appTooltip]="'Clear search'"
                appTooltipPosition="top"
              >
                <ng-icon name="heroXMark" class="size-5" />
              </button>
            }
          </div>

          <select
            [ngModel]="enabledFilter()"
            (ngModelChange)="enabledFilter.set($event)"
            class="rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All Connectors</option>
            <option value="enabled">Enabled Only</option>
            <option value="disabled">Disabled Only</option>
          </select>

          <select
            [ngModel]="typeFilter()"
            (ngModelChange)="typeFilter.set($event)"
            class="rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All Types</option>
            <option value="google">Google</option>
            <option value="microsoft">Microsoft</option>
            <option value="github">GitHub</option>
            <option value="canvas">Canvas LMS</option>
            <option value="custom">Custom</option>
          </select>

          @if (hasActiveFilters()) {
            <button
              (click)="resetFilters()"
              class="text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark"
            >
              Clear Filters
            </button>
          }
        </div>

        <!-- Loading State -->
        @if (connectorsResource.isLoading() && connectors().length === 0) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <app-spinner size="xl" label="Loading connectors" />
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Loading connectors...
              </p>
            </div>
          </div>
        }

        <!-- Error State -->
        @if (connectorsResource.error()) {
          <div class="mb-6 rounded-sm border border-state-danger-200 bg-state-danger-50 p-4 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
            <p class="font-medium">Failed to load connectors</p>
            <p class="mt-1 text-sm/6">Please check your connection and try again.</p>
            <button
              (click)="connectorsService.reload()"
              class="mt-3 text-sm/6 font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        }

        <!-- Connectors Table -->
        @if (!connectorsResource.isLoading() || connectors().length > 0) {
          <div class="overflow-hidden rounded-sm border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead class="bg-gray-50 dark:bg-gray-800/50">
                <tr>
                  <th scope="col" class="py-3.5 pl-4 pr-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 sm:pl-6">
                    Connector
                  </th>
                  <th scope="col" class="hidden px-3 py-3.5 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 md:table-cell">
                    Type
                  </th>
                  <th scope="col" class="hidden px-3 py-3.5 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 lg:table-cell">
                    Scopes
                  </th>
                  <th scope="col" class="hidden px-3 py-3.5 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 sm:table-cell">
                    Access
                  </th>
                  <th scope="col" class="px-3 py-3.5 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                    Status
                  </th>
                  <th scope="col" class="relative py-3.5 pl-3 pr-4 sm:pr-6">
                    <span class="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                @for (connector of filteredConnectors(); track connector.providerId) {
                  <tr
                    class="transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/50"
                    [class.opacity-60]="!connector.enabled"
                  >
                    <!-- Connector Info -->
                    <td class="whitespace-nowrap py-4 pl-4 pr-3 sm:pl-6">
                      <div class="flex items-center gap-3">
                        @if (connector.iconData) {
                          <div class="flex size-10 shrink-0 items-center justify-center rounded-sm bg-gray-50 dark:bg-gray-900">
                            <img
                              [src]="connector.iconData"
                              [alt]="connector.displayName + ' icon'"
                              class="size-7 object-contain"
                            />
                          </div>
                        } @else {
                          <div [class]="getConnectorIconClasses(connector.providerType)">
                            <ng-icon [name]="getConnectorIcon(connector)" class="size-5" />
                          </div>
                        }
                        <div class="min-w-0">
                          <p class="font-medium text-gray-900 dark:text-white">
                            {{ connector.displayName }}
                          </p>
                          <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                            {{ connector.providerId }}
                          </p>
                        </div>
                      </div>
                    </td>

                    <!-- Type -->
                    <td class="hidden whitespace-nowrap px-3 py-4 md:table-cell">
                      <span [class]="getConnectorTypeBadgeClasses(connector.providerType)">
                        {{ getConnectorTypeLabel(connector.providerType) }}
                      </span>
                    </td>

                    <!-- Scopes -->
                    <td class="hidden px-3 py-4 lg:table-cell">
                      <div class="flex flex-wrap gap-1">
                        @if (connector.scopes.length === 0) {
                          <span class="text-sm/6 text-gray-400 dark:text-gray-500">None</span>
                        } @else {
                          <span
                            class="cursor-help text-sm/6 text-gray-600 dark:text-gray-300"
                            [appTooltip]="connector.scopes.join(', ')"
                            appTooltipPosition="top"
                          >
                            {{ connector.scopes.length }} scope{{ connector.scopes.length === 1 ? '' : 's' }}
                          </span>
                        }
                      </div>
                    </td>

                    <!-- Access -->
                    <td class="hidden whitespace-nowrap px-3 py-4 sm:table-cell">
                      @if (connector.allowedRoles.length === 0 || connector.allowedRoles.includes('*')) {
                        <span class="inline-flex items-center gap-1 text-sm/6 text-category-accent-skills-600 dark:text-category-accent-skills-400">
                          <ng-icon name="heroShieldCheck" class="size-4" />
                          All Roles
                        </span>
                      } @else {
                        <span
                          class="cursor-help text-sm/6 text-gray-600 dark:text-gray-300"
                          [appTooltip]="connector.allowedRoles.join(', ')"
                          appTooltipPosition="top"
                        >
                          {{ connector.allowedRoles.length }} role{{ connector.allowedRoles.length === 1 ? '' : 's' }}
                        </span>
                      }
                    </td>

                    <!-- Status -->
                    <td class="whitespace-nowrap px-3 py-4">
                      @if (connector.enabled) {
                        <span class="inline-flex items-center gap-1 rounded-xs bg-state-success-100 px-2 py-0.5 text-xs font-medium text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300">
                          <ng-icon name="heroCheck" class="size-3" />
                          Active
                        </span>
                      } @else {
                        <span class="inline-flex items-center gap-1 rounded-xs bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                          <ng-icon name="heroXCircle" class="size-3" />
                          Disabled
                        </span>
                      }
                    </td>

                    <!-- Actions -->
                    <td class="whitespace-nowrap py-4 pl-3 pr-4 text-right sm:pr-6">
                      <div class="flex items-center justify-end gap-2">
                        <a
                          [routerLink]="['/admin/connectors/edit', connector.providerId]"
                          class="inline-flex items-center justify-center rounded-xs p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                          [appTooltip]="'Edit connector'"
                          appTooltipPosition="top"
                        >
                          <ng-icon name="heroPencilSquare" class="size-5" />
                        </a>
                        <button
                          (click)="deleteConnector(connector)"
                          class="inline-flex items-center justify-center rounded-xs p-2 text-gray-500 hover:bg-state-danger-50 hover:text-state-danger-600 focus:outline-hidden focus:ring-3 focus:ring-state-danger-500/50 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                          [appTooltip]="'Delete connector'"
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
          @if (filteredConnectors().length === 0 && !connectorsResource.isLoading()) {
            <div class="py-16 text-center">
              <div class="mx-auto mb-4 flex size-16 items-center justify-center rounded-full bg-gray-100 dark:bg-gray-800">
                <ng-icon name="heroLink" class="size-8 text-gray-400" />
              </div>
              @if (hasActiveFilters()) {
                <h3 class="text-lg/7 font-medium text-gray-900 dark:text-white">No connectors match your filters</h3>
                <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">Try adjusting your search or filter criteria.</p>
                <button
                  (click)="resetFilters()"
                  class="mt-4 text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark"
                >
                  Clear all filters
                </button>
              } @else {
                <h3 class="text-lg/7 font-medium text-gray-900 dark:text-white">No connectors configured</h3>
                <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
                  Get started by adding your first connector.
                </p>
                <a
                  routerLink="/admin/connectors/new"
                  class="mt-6 inline-flex items-center gap-2 rounded-sm bg-primary-accessible px-4 py-2.5 text-sm/6 font-semibold text-white hover:brightness-95 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50"
                >
                  <ng-icon name="heroPlus" class="size-5" />
                  Add Connector
                </a>
              }
            </div>
          }
        }

        <!-- Info Section -->
        @if (connectors().length > 0) {
          <div class="mt-8 rounded-sm border border-state-info-200 bg-state-info-50 p-6 dark:border-state-info-800 dark:bg-state-info-900/20">
            <h2 class="text-lg/7 font-semibold text-state-info-900 dark:text-state-info-200">About Connectors</h2>
            <div class="mt-3 space-y-2 text-sm/6 text-state-info-800 dark:text-state-info-300">
              <p>
                <strong>Connector Types:</strong> Choose from common presets (Google, Microsoft, GitHub, Canvas) or configure a custom OAuth 2.0 connector.
              </p>
              <p>
                <strong>Role Restrictions:</strong> Control which application roles can use each connector. Leave empty for unrestricted access.
              </p>
              <p>
                <strong>Security:</strong> Client secrets are encrypted and stored securely. They are never exposed to the frontend after creation.
              </p>
            </div>
          </div>
        }
    </div>
  `,
})
export class ConnectorListPage {
  connectorsService = inject(ConnectorsService);
  private router = inject(Router);
  private dialog = inject(Dialog);

  readonly connectorsResource = this.connectorsService.connectorsResource;

  searchQuery = signal('');
  enabledFilter = signal('');
  typeFilter = signal('');

  readonly connectors = computed(() => this.connectorsService.getConnectors());

  readonly filteredConnectors = computed(() => {
    let connectors = this.connectors();
    const query = this.searchQuery().toLowerCase();
    const enabled = this.enabledFilter();
    const type = this.typeFilter();

    if (query) {
      connectors = connectors.filter(
        c =>
          c.displayName.toLowerCase().includes(query) ||
          c.providerId.toLowerCase().includes(query) ||
          c.providerType.toLowerCase().includes(query)
      );
    }

    if (enabled === 'enabled') {
      connectors = connectors.filter(c => c.enabled);
    } else if (enabled === 'disabled') {
      connectors = connectors.filter(c => !c.enabled);
    }

    if (type) {
      connectors = connectors.filter(c => c.providerType === type);
    }

    return connectors.sort((a, b) => {
      if (a.enabled !== b.enabled) {
        return a.enabled ? -1 : 1;
      }
      return a.displayName.localeCompare(b.displayName);
    });
  });

  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.enabledFilter() || this.typeFilter());
  });

  resetFilters(): void {
    this.searchQuery.set('');
    this.enabledFilter.set('');
    this.typeFilter.set('');
  }

  getConnectorIcon(connector: Connector): string {
    if (connector.iconName) {
      return connector.iconName;
    }
    switch (connector.providerType) {
      case 'google':
      case 'microsoft':
        return 'heroCloud';
      case 'github':
        return 'heroCodeBracket';
      case 'canvas':
        return 'heroAcademicCap';
      default:
        return 'heroLink';
    }
  }

  getConnectorIconClasses(type: ConnectorType): string {
    const baseClasses = 'flex size-10 shrink-0 items-center justify-center rounded-sm';
    switch (type) {
      case 'google':
        // Matches the vendor-google mapping already established in
        // settings/connectors-settings.page.ts for the same providerType.
        return `${baseClasses} bg-vendor-google-100 text-vendor-google-600 dark:bg-vendor-google-900/30 dark:text-vendor-google-400`;
      case 'microsoft':
        return `${baseClasses} bg-vendor-microsoft-100 text-vendor-microsoft-600 dark:bg-vendor-microsoft-900/30 dark:text-vendor-microsoft-400`;
      case 'github':
        return `${baseClasses} bg-gray-800 text-white dark:bg-gray-700`;
      case 'canvas':
        return `${baseClasses} bg-vendor-canvas-100 text-vendor-canvas-600 dark:bg-vendor-canvas-900/30 dark:text-vendor-canvas-400`;
      default:
        return `${baseClasses} bg-vendor-generic-100 text-vendor-generic-600 dark:bg-vendor-generic-900/30 dark:text-vendor-generic-400`;
    }
  }

  getConnectorTypeBadgeClasses(type: ConnectorType): string {
    const baseClasses = 'inline-flex items-center rounded-xs px-2 py-0.5 text-xs font-medium';
    switch (type) {
      case 'google':
        return `${baseClasses} bg-vendor-google-100 text-vendor-google-700 dark:bg-vendor-google-900/30 dark:text-vendor-google-300`;
      case 'microsoft':
        return `${baseClasses} bg-vendor-microsoft-100 text-vendor-microsoft-700 dark:bg-vendor-microsoft-900/30 dark:text-vendor-microsoft-400`;
      case 'github':
        return `${baseClasses} bg-gray-800 text-white dark:bg-gray-700`;
      case 'canvas':
        return `${baseClasses} bg-vendor-canvas-100 text-vendor-canvas-700 dark:bg-vendor-canvas-900/30 dark:text-vendor-canvas-300`;
      default:
        return `${baseClasses} bg-vendor-generic-100 text-vendor-generic-700 dark:bg-vendor-generic-900/30 dark:text-vendor-generic-300`;
    }
  }

  getConnectorTypeLabel(type: ConnectorType): string {
    switch (type) {
      case 'google':
        return 'Google';
      case 'microsoft':
        return 'Microsoft';
      case 'github':
        return 'GitHub';
      case 'canvas':
        return 'Canvas LMS';
      default:
        return 'Custom';
    }
  }

  async deleteConnector(connector: Connector): Promise<void> {
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: `Delete ${connector.displayName}`,
        message:
          `This will disconnect all users currently using this connector ` +
          `and delete it from AgentCore Identity. This action cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed !== true) return;

    try {
      await this.connectorsService.deleteConnector(connector.providerId);
    } catch (error: any) {
      console.error('Error deleting connector:', error);
      const message = error?.error?.detail || error?.message || 'Failed to delete connector.';
      alert(message);
    }
  }
}
