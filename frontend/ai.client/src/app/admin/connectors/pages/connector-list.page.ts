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
  heroShieldCheck,
} from '@ng-icons/heroicons/outline';
import { ConnectorsService } from '../services/connectors.service';
import { Connector, getConnectorTypeLabel } from '../models/connector.model';
import { ConnectorLogoComponent } from '../components/connector-logo.component';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

@Component({
  selector: 'app-connector-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon, TooltipDirective, SpinnerComponent, ConnectorLogoComponent],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroTrash,
      heroXMark,
      heroShieldCheck,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Page header -->
        <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Connectors</h1>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Configure the OAuth providers users can connect for tool authentication.
            </p>
          </div>
          <a
            routerLink="/admin/connectors/new"
            class="inline-flex shrink-0 items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:brightness-110"
          >
            <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
            Add connector
          </a>
        </div>

        <!-- Toolbar -->
        <div class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
          <div class="relative flex-1">
            <ng-icon
              name="heroMagnifyingGlass"
              class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <label for="search" class="sr-only">Search connectors</label>
            <input
              type="text"
              id="search"
              [ngModel]="searchQuery()"
              (ngModelChange)="searchQuery.set($event)"
              placeholder="Search by name, ID, or type…"
              class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-9 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            />
            @if (searchQuery()) {
              <button
                type="button"
                (click)="searchQuery.set('')"
                aria-label="Clear search"
                class="absolute right-1.5 top-1/2 flex size-7 -translate-y-1/2 items-center justify-center rounded-2xl text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                [appTooltip]="'Clear search'"
                appTooltipPosition="top"
              >
                <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
              </button>
            }
          </div>

          <label for="status" class="sr-only">Filter by status</label>
          <select
            id="status"
            [ngModel]="enabledFilter()"
            (ngModelChange)="enabledFilter.set($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All statuses</option>
            <option value="enabled">Enabled</option>
            <option value="disabled">Disabled</option>
          </select>

          <label for="type" class="sr-only">Filter by type</label>
          <select
            id="type"
            [ngModel]="typeFilter()"
            (ngModelChange)="typeFilter.set($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All types</option>
            @for (option of typeOptions(); track option.value) {
              <option [value]="option.value">{{ option.label }}</option>
            }
          </select>

          @if (hasActiveFilters()) {
            <button
              type="button"
              (click)="resetFilters()"
              class="rounded-2xl px-3 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
            >
              Reset
            </button>
          }
        </div>

        <!-- Count -->
        <div class="mb-3 text-xs/5 text-gray-500 dark:text-gray-400">
          {{ filteredConnectors().length }} connector{{ filteredConnectors().length !== 1 ? 's' : '' }}
        </div>

        <!-- Loading -->
        @if (connectorsResource.isLoading() && connectors().length === 0) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <app-spinner size="xl" label="Loading connectors" />
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading connectors…</p>
            </div>
          </div>
        }

        <!-- Error -->
        @if (connectorsResource.error()) {
          <div class="mb-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 p-4 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
            <p class="text-sm/6 font-medium">Failed to load connectors</p>
            <p class="mt-1 text-sm/6">Please check your connection and try again.</p>
            <button
              type="button"
              (click)="connectorsService.reload()"
              class="mt-2 text-sm/6 font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        }

        <!-- List -->
        @if (!connectorsResource.isLoading() || connectors().length > 0) {
          @if (filteredConnectors().length === 0) {
            <div class="rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
              @if (hasActiveFilters()) {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">No connectors match the current filters.</p>
              } @else {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">No connectors configured yet.</p>
                <a
                  routerLink="/admin/connectors/new"
                  class="mt-4 inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:brightness-110"
                >
                  <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
                  Add connector
                </a>
              }
            </div>
          } @else {
            <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
              @for (connector of filteredConnectors(); track connector.providerId) {
                <li class="flex items-center gap-3 px-3 py-3 sm:px-4" [class.opacity-60]="!connector.enabled">
                  <a
                    [routerLink]="['/admin/connectors/edit', connector.providerId]"
                    class="flex min-w-0 flex-1 items-center gap-3 rounded-2xl focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                  >
                    <app-connector-logo
                      [type]="connector.providerType"
                      [iconData]="connector.iconData"
                      [iconName]="connector.iconName"
                      size="md"
                    />
                    <span class="min-w-0">
                      <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ connector.displayName }}</span>
                      <span class="block truncate font-mono text-xs/5 text-gray-500 dark:text-gray-400">{{ connector.providerId }}</span>
                    </span>
                  </a>

                  <span class="hidden shrink-0 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium text-gray-600 md:inline-block dark:bg-gray-700 dark:text-gray-300">
                    {{ typeLabel(connector.providerType) }}
                  </span>

                  <span class="hidden w-20 shrink-0 text-xs/5 text-gray-600 lg:block dark:text-gray-400">
                    @if (connector.scopes.length === 0) {
                      No scopes
                    } @else {
                      <span class="cursor-help" [appTooltip]="connector.scopes.join(', ')" appTooltipPosition="top">
                        {{ connector.scopes.length }} scope{{ connector.scopes.length === 1 ? '' : 's' }}
                      </span>
                    }
                  </span>

                  <span class="hidden w-24 shrink-0 text-xs/5 text-gray-600 sm:block dark:text-gray-400">
                    @if (connector.allowedRoles.length === 0 || connector.allowedRoles.includes('*')) {
                      <span class="inline-flex items-center gap-1">
                        <ng-icon name="heroShieldCheck" class="size-4" aria-hidden="true" />
                        All roles
                      </span>
                    } @else {
                      <span class="cursor-help" [appTooltip]="connector.allowedRoles.join(', ')" appTooltipPosition="top">
                        {{ connector.allowedRoles.length }} role{{ connector.allowedRoles.length === 1 ? '' : 's' }}
                      </span>
                    }
                  </span>

                  @if (connector.enabled) {
                    <span class="shrink-0 rounded-full bg-state-success-100 px-2.5 py-0.5 text-xs/5 font-medium text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300">Active</span>
                  } @else {
                    <span class="shrink-0 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">Disabled</span>
                  }

                  <div class="flex shrink-0 items-center gap-1">
                    <a
                      [routerLink]="['/admin/connectors/edit', connector.providerId]"
                      [attr.aria-label]="'Edit ' + connector.displayName"
                      class="flex size-8 items-center justify-center rounded-2xl text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      [appTooltip]="'Edit connector'"
                      appTooltipPosition="top"
                    >
                      <ng-icon name="heroPencilSquare" class="size-5" aria-hidden="true" />
                    </a>
                    <button
                      type="button"
                      (click)="deleteConnector(connector)"
                      [attr.aria-label]="'Delete ' + connector.displayName"
                      class="flex size-8 items-center justify-center rounded-2xl text-gray-500 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                      [appTooltip]="'Delete connector'"
                      appTooltipPosition="top"
                    >
                      <ng-icon name="heroTrash" class="size-5" aria-hidden="true" />
                    </button>
                  </div>
                </li>
              }
            </ul>
            <p class="mt-4 text-xs/5 text-gray-500 dark:text-gray-400">
              Client secrets are stored in AgentCore Identity and are never sent back to the browser.
            </p>
          }
        }
      </div>
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

  /** Types present among the configured connectors, labelled and sorted. */
  readonly typeOptions = computed(() => {
    const types = new Set(this.connectors().map(c => c.providerType));
    return [...types]
      .map(value => ({ value, label: getConnectorTypeLabel(value) }))
      .sort((a, b) => a.label.localeCompare(b.label));
  });

  resetFilters(): void {
    this.searchQuery.set('');
    this.enabledFilter.set('');
    this.typeFilter.set('');
  }

  readonly typeLabel = getConnectorTypeLabel;

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
