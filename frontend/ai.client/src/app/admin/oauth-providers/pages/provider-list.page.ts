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
import { OAuthProvidersService } from '../services/oauth-providers.service';
import { OAuthProvider, OAuthProviderType } from '../models/oauth-provider.model';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';

@Component({
  selector: 'app-provider-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon, TooltipDirective],
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
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Back Button -->
        <a
          routerLink="/admin"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Admin
        </a>

        <!-- Page Header -->
        <div class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">OAuth Providers</h1>
            <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
              Configure third-party OAuth integrations for MCP tool authentication.
            </p>
          </div>
          <a
            routerLink="/admin/oauth-providers/new"
            class="inline-flex items-center justify-center gap-2 rounded-sm bg-blue-600 px-4 py-2.5 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            <ng-icon name="heroPlus" class="size-5" />
            Add Provider
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
              placeholder="Search providers..."
              class="w-full rounded-sm border border-gray-300 bg-white py-2.5 pl-10 pr-10 text-sm/6 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
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
            <option value="">All Providers</option>
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
              class="text-sm/6 font-medium text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
            >
              Clear Filters
            </button>
          }
        </div>

        <!-- Loading State -->
        @if (providersResource.isLoading() && providers().length === 0) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div
                class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600"
              ></div>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Loading providers...
              </p>
            </div>
          </div>
        }

        <!-- Error State -->
        @if (providersResource.error()) {
          <div class="mb-6 rounded-sm border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200">
            <p class="font-medium">Failed to load providers</p>
            <p class="mt-1 text-sm/6">Please check your connection and try again.</p>
            <button
              (click)="oauthProvidersService.reload()"
              class="mt-3 text-sm/6 font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        }

        <!-- Providers Table -->
        @if (!providersResource.isLoading() || providers().length > 0) {
          <div class="overflow-hidden rounded-sm border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead class="bg-gray-50 dark:bg-gray-800/50">
                <tr>
                  <th scope="col" class="py-3.5 pl-4 pr-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 sm:pl-6">
                    Provider
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
                @for (provider of filteredProviders(); track provider.providerId) {
                  <tr
                    class="transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/50"
                    [class.opacity-60]="!provider.enabled"
                  >
                    <!-- Provider Info -->
                    <td class="whitespace-nowrap py-4 pl-4 pr-3 sm:pl-6">
                      <div class="flex items-center gap-3">
                        <div [class]="getProviderIconClasses(provider.providerType)">
                          <ng-icon [name]="getProviderIcon(provider)" class="size-5" />
                        </div>
                        <div class="min-w-0">
                          <p class="font-medium text-gray-900 dark:text-white">
                            {{ provider.displayName }}
                          </p>
                          <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                            {{ provider.providerId }}
                          </p>
                        </div>
                      </div>
                    </td>

                    <!-- Type -->
                    <td class="hidden whitespace-nowrap px-3 py-4 md:table-cell">
                      <span [class]="getProviderTypeBadgeClasses(provider.providerType)">
                        {{ getProviderTypeLabel(provider.providerType) }}
                      </span>
                    </td>

                    <!-- Scopes -->
                    <td class="hidden px-3 py-4 lg:table-cell">
                      <div class="flex flex-wrap gap-1">
                        @if (provider.scopes.length === 0) {
                          <span class="text-sm/6 text-gray-400 dark:text-gray-500">None</span>
                        } @else {
                          <span
                            class="cursor-help text-sm/6 text-gray-600 dark:text-gray-300"
                            [appTooltip]="provider.scopes.join(', ')"
                            appTooltipPosition="top"
                          >
                            {{ provider.scopes.length }} scope{{ provider.scopes.length === 1 ? '' : 's' }}
                          </span>
                        }
                      </div>
                    </td>

                    <!-- Access -->
                    <td class="hidden whitespace-nowrap px-3 py-4 sm:table-cell">
                      @if (provider.allowedRoles.length === 0 || provider.allowedRoles.includes('*')) {
                        <span class="inline-flex items-center gap-1 text-sm/6 text-purple-600 dark:text-purple-400">
                          <ng-icon name="heroShieldCheck" class="size-4" />
                          All Roles
                        </span>
                      } @else {
                        <span
                          class="cursor-help text-sm/6 text-gray-600 dark:text-gray-300"
                          [appTooltip]="provider.allowedRoles.join(', ')"
                          appTooltipPosition="top"
                        >
                          {{ provider.allowedRoles.length }} role{{ provider.allowedRoles.length === 1 ? '' : 's' }}
                        </span>
                      }
                    </td>

                    <!-- Status -->
                    <td class="whitespace-nowrap px-3 py-4">
                      @if (provider.enabled) {
                        <span class="inline-flex items-center gap-1 rounded-xs bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-300">
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
                          [routerLink]="['/admin/oauth-providers/edit', provider.providerId]"
                          class="inline-flex items-center justify-center rounded-xs p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                          [appTooltip]="'Edit provider'"
                          appTooltipPosition="top"
                        >
                          <ng-icon name="heroPencilSquare" class="size-5" />
                        </a>
                        <button
                          (click)="deleteProvider(provider)"
                          class="inline-flex items-center justify-center rounded-xs p-2 text-gray-500 hover:bg-red-50 hover:text-red-600 focus:outline-hidden focus:ring-3 focus:ring-red-500/50 dark:text-gray-400 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                          [appTooltip]="'Delete provider'"
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
          @if (filteredProviders().length === 0 && !providersResource.isLoading()) {
            <div class="py-16 text-center">
              <div class="mx-auto mb-4 flex size-16 items-center justify-center rounded-full bg-gray-100 dark:bg-gray-800">
                <ng-icon name="heroLink" class="size-8 text-gray-400" />
              </div>
              @if (hasActiveFilters()) {
                <h3 class="text-lg/7 font-medium text-gray-900 dark:text-white">No providers match your filters</h3>
                <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">Try adjusting your search or filter criteria.</p>
                <button
                  (click)="resetFilters()"
                  class="mt-4 text-sm/6 font-medium text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  Clear all filters
                </button>
              } @else {
                <h3 class="text-lg/7 font-medium text-gray-900 dark:text-white">No OAuth providers configured</h3>
                <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
                  Get started by adding your first OAuth provider integration.
                </p>
                <a
                  routerLink="/admin/oauth-providers/new"
                  class="mt-6 inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2.5 text-sm/6 font-semibold text-white hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50"
                >
                  <ng-icon name="heroPlus" class="size-5" />
                  Add Provider
                </a>
              }
            </div>
          }
        }

        <!-- Info Section -->
        @if (providers().length > 0) {
          <div class="mt-8 rounded-sm border border-blue-200 bg-blue-50 p-6 dark:border-blue-800 dark:bg-blue-900/20">
            <h2 class="text-lg/7 font-semibold text-blue-900 dark:text-blue-200">About OAuth Providers</h2>
            <div class="mt-3 space-y-2 text-sm/6 text-blue-800 dark:text-blue-300">
              <p>
                <strong>Provider Types:</strong> Choose from common presets (Google, Microsoft, GitHub, Canvas) or configure a custom OAuth 2.0 provider.
              </p>
              <p>
                <strong>Role Restrictions:</strong> Control which application roles can connect to each provider. Leave empty for unrestricted access.
              </p>
              <p>
                <strong>Security:</strong> Client secrets are encrypted and stored securely. They are never exposed to the frontend after creation.
              </p>
            </div>
          </div>
        }
      </div>
    </div>
  `,
})
export class ProviderListPage {
  oauthProvidersService = inject(OAuthProvidersService);
  private router = inject(Router);

  readonly providersResource = this.oauthProvidersService.providersResource;

  // Local state
  searchQuery = signal('');
  enabledFilter = signal('');
  typeFilter = signal('');

  // Computed
  readonly providers = computed(() => this.oauthProvidersService.getProviders());

  readonly filteredProviders = computed(() => {
    let providers = this.providers();
    const query = this.searchQuery().toLowerCase();
    const enabled = this.enabledFilter();
    const type = this.typeFilter();

    if (query) {
      providers = providers.filter(
        p =>
          p.displayName.toLowerCase().includes(query) ||
          p.providerId.toLowerCase().includes(query) ||
          p.providerType.toLowerCase().includes(query)
      );
    }

    if (enabled === 'enabled') {
      providers = providers.filter(p => p.enabled);
    } else if (enabled === 'disabled') {
      providers = providers.filter(p => !p.enabled);
    }

    if (type) {
      providers = providers.filter(p => p.providerType === type);
    }

    // Sort: enabled first, then alphabetically by display name
    return providers.sort((a, b) => {
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

  getProviderIcon(provider: OAuthProvider): string {
    if (provider.iconName) {
      return provider.iconName;
    }
    // Default icons by type
    switch (provider.providerType) {
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

  getProviderIconClasses(type: OAuthProviderType): string {
    const baseClasses = 'flex size-10 shrink-0 items-center justify-center rounded-sm';
    switch (type) {
      case 'google':
        return `${baseClasses} bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400`;
      case 'microsoft':
        return `${baseClasses} bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400`;
      case 'github':
        return `${baseClasses} bg-gray-800 text-white dark:bg-gray-700`;
      case 'canvas':
        return `${baseClasses} bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400`;
      default:
        return `${baseClasses} bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400`;
    }
  }

  getProviderTypeBadgeClasses(type: OAuthProviderType): string {
    const baseClasses = 'inline-flex items-center rounded-xs px-2 py-0.5 text-xs font-medium';
    switch (type) {
      case 'google':
        return `${baseClasses} bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300`;
      case 'microsoft':
        return `${baseClasses} bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300`;
      case 'github':
        return `${baseClasses} bg-gray-800 text-white dark:bg-gray-700`;
      case 'canvas':
        return `${baseClasses} bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300`;
      default:
        return `${baseClasses} bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300`;
    }
  }

  getProviderTypeLabel(type: OAuthProviderType): string {
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

  async deleteProvider(provider: OAuthProvider): Promise<void> {
    if (!confirm(`Are you sure you want to delete the provider "${provider.displayName}"?\n\nThis will disconnect all users currently using this provider. This action cannot be undone.`)) {
      return;
    }

    try {
      await this.oauthProvidersService.deleteProvider(provider.providerId);
    } catch (error: any) {
      console.error('Error deleting provider:', error);
      const message = error?.error?.detail || error?.message || 'Failed to delete provider.';
      alert(message);
    }
  }
}
