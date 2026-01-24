import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { Router, RouterLink, ActivatedRoute } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroLink,
  heroCloud,
  heroCodeBracket,
  heroAcademicCap,
  heroCheck,
  heroExclamationTriangle,
  heroArrowPath,
} from '@ng-icons/heroicons/outline';
import { ConnectionsService } from './services';
import { OAuthConnection, OAuthProviderType } from './models';
import { ToastService } from '../../services/toast/toast.service';

@Component({
  selector: 'app-connections',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroLink,
      heroCloud,
      heroCodeBracket,
      heroAcademicCap,
      heroCheck,
      heroExclamationTriangle,
      heroArrowPath,
    }),
  ],
  host: {
    class: 'block',
  },
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Back Button -->
        <a
          routerLink="/"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Chat
        </a>

        <!-- Page Header -->
        <div class="mb-8">
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Connected Apps</h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            Connect your accounts to enable tools that require third-party authentication.
          </p>
        </div>

        <!-- Loading State -->
        @if (connectionsResource.isLoading() && connections().length === 0) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div
                class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600"
              ></div>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Loading connections...
              </p>
            </div>
          </div>
        }

        <!-- Error State -->
        @if (connectionsResource.error()) {
          <div class="mb-6 rounded-sm border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200">
            <p class="font-medium">Failed to load connections</p>
            <p class="mt-1 text-sm/6">Please check your connection and try again.</p>
            <button
              (click)="connectionsService.reload()"
              class="mt-3 text-sm/6 font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        }

        <!-- Connections Grid -->
        @if (!connectionsResource.isLoading() || connections().length > 0) {
          @if (connections().length === 0) {
            <!-- Empty State -->
            <div class="py-16 text-center">
              <div class="mx-auto mb-4 flex size-16 items-center justify-center rounded-full bg-gray-100 dark:bg-gray-800">
                <ng-icon name="heroLink" class="size-8 text-gray-400" />
              </div>
              <h3 class="text-lg/7 font-medium text-gray-900 dark:text-white">No connections available</h3>
              <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
                There are no OAuth providers configured for your account. Contact an administrator if you need access to external tools.
              </p>
            </div>
          } @else {
            <div class="grid gap-4 sm:grid-cols-2">
              @for (connection of connections(); track connection.providerId) {
                <div
                  class="flex flex-col rounded-sm border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800"
                >
                  <!-- Provider Info -->
                  <div class="flex items-start gap-4">
                    <div [class]="getProviderIconClasses(connection.providerType)">
                      <ng-icon [name]="getProviderIcon(connection)" class="size-6" />
                    </div>
                    <div class="min-w-0 flex-1">
                      <h3 class="font-semibold text-gray-900 dark:text-white">
                        {{ connection.displayName }}
                      </h3>

                      <!-- Status Badge -->
                      @if (isConnected(connection)) {
                        <div class="mt-1 flex items-center gap-1.5">
                          <span class="inline-flex items-center gap-1 rounded-xs bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-300">
                            <ng-icon name="heroCheck" class="size-3" />
                            Connected
                          </span>
                          @if (connection.connectedAt) {
                            <span class="text-xs text-gray-500 dark:text-gray-400">
                              since {{ formatDate(connection.connectedAt) }}
                            </span>
                          }
                        </div>
                      } @else if (connection.needsReauth || connection.status === 'needs_reauth' || connection.status === 'expired') {
                        <div class="mt-1">
                          <span class="inline-flex items-center gap-1 rounded-xs bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-300">
                            <ng-icon name="heroExclamationTriangle" class="size-3" />
                            Needs Re-authorization
                          </span>
                        </div>
                      } @else {
                        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                          Not connected
                        </p>
                      }
                    </div>
                  </div>

                  <!-- Action Button -->
                  <div class="mt-4 flex justify-end">
                    @if (isConnected(connection) && !connection.needsReauth && connection.status !== 'needs_reauth' && connection.status !== 'expired') {
                      <button
                        (click)="disconnect(connection)"
                        [disabled]="disconnecting() === connection.providerId"
                        class="inline-flex items-center gap-2 rounded-xs border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                      >
                        @if (disconnecting() === connection.providerId) {
                          <ng-icon name="heroArrowPath" class="size-4 animate-spin" />
                          Disconnecting...
                        } @else {
                          Disconnect
                        }
                      </button>
                    } @else {
                      <button
                        (click)="connect(connection)"
                        [disabled]="connecting() === connection.providerId"
                        class="inline-flex items-center gap-2 rounded-xs bg-blue-600 px-3 py-1.5 text-sm/6 font-semibold text-white hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                      >
                        @if (connecting() === connection.providerId) {
                          <ng-icon name="heroArrowPath" class="size-4 animate-spin" />
                          Connecting...
                        } @else {
                          @if (connection.needsReauth || connection.status === 'needs_reauth' || connection.status === 'expired') {
                            Reconnect
                          } @else {
                            Connect
                          }
                        }
                      </button>
                    }
                  </div>
                </div>
              }
            </div>
          }
        }

        <!-- Info Section -->
        @if (connections().length > 0) {
          <div class="mt-8 rounded-sm border border-blue-200 bg-blue-50 p-6 dark:border-blue-800 dark:bg-blue-900/20">
            <h2 class="text-lg/7 font-semibold text-blue-900 dark:text-blue-200">About Connections</h2>
            <div class="mt-3 space-y-2 text-sm/6 text-blue-800 dark:text-blue-300">
              <p>
                <strong>Connected apps</strong> allow certain tools to access external services on your behalf.
              </p>
              <p>
                <strong>Re-authorization</strong> may be required if the app's permissions change or your token expires.
              </p>
              <p>
                <strong>Disconnecting</strong> will revoke the app's access to your account. You can reconnect at any time.
              </p>
            </div>
          </div>
        }
      </div>
    </div>
  `,
})
export class ConnectionsPage implements OnInit {
  connectionsService = inject(ConnectionsService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private toast = inject(ToastService);

  readonly connectionsResource = this.connectionsService.connectionsResource;

  // Local state
  connecting = signal<string | null>(null);
  disconnecting = signal<string | null>(null);

  // Computed
  readonly connections = computed(() => this.connectionsService.getConnections());

  ngOnInit(): void {
    this.handleCallbackParams();
  }

  /**
   * Handle OAuth callback query parameters.
   */
  private handleCallbackParams(): void {
    const params = this.route.snapshot.queryParams;

    if (params['success'] === 'true') {
      const provider = params['provider'] || 'the service';
      this.toast.success('Connected!', `Successfully connected to ${provider}.`);
      // Refresh connections after successful OAuth
      this.connectionsService.reload();
      // Clear query params
      this.router.navigate([], {
        relativeTo: this.route,
        queryParams: {},
        replaceUrl: true,
      });
    } else if (params['error']) {
      const error = params['error'];
      const provider = params['provider'] || 'the service';
      const description = params['error_description'];

      let message = `Failed to connect to ${provider}.`;
      if (description) {
        message = description;
      } else if (error === 'access_denied') {
        message = 'Authorization was denied. Please try again.';
      } else if (error === 'missing_params') {
        message = 'Invalid callback. Please try again.';
      }

      this.toast.error('Connection Failed', message);
      // Clear query params
      this.router.navigate([], {
        relativeTo: this.route,
        queryParams: {},
        replaceUrl: true,
      });
    }
  }

  /**
   * Check if a connection is actively connected.
   */
  isConnected(connection: OAuthConnection): boolean {
    return connection.status === 'connected';
  }

  /**
   * Initiate OAuth connection flow.
   */
  async connect(connection: OAuthConnection): Promise<void> {
    this.connecting.set(connection.providerId);

    try {
      const redirectUrl = window.location.origin + '/settings/oauth/callback';
      const authUrl = await this.connectionsService.connect(connection.providerId, redirectUrl);
      // Redirect to OAuth authorization
      window.location.href = authUrl;
    } catch (error: any) {
      console.error('Error initiating connection:', error);
      const message = error?.error?.detail || error?.message || 'Failed to initiate connection.';
      this.toast.error('Connection Error', message);
      this.connecting.set(null);
    }
  }

  /**
   * Disconnect from a provider.
   */
  async disconnect(connection: OAuthConnection): Promise<void> {
    if (!confirm(`Are you sure you want to disconnect from ${connection.displayName}?`)) {
      return;
    }

    this.disconnecting.set(connection.providerId);

    try {
      await this.connectionsService.disconnect(connection.providerId);
      this.toast.success('Disconnected', `Successfully disconnected from ${connection.displayName}.`);
    } catch (error: any) {
      console.error('Error disconnecting:', error);
      const message = error?.error?.detail || error?.message || 'Failed to disconnect.';
      this.toast.error('Disconnect Error', message);
    } finally {
      this.disconnecting.set(null);
    }
  }

  /**
   * Get icon name for a provider.
   */
  getProviderIcon(connection: OAuthConnection): string {
    if (connection.iconName && connection.iconName !== 'heroLink') {
      return connection.iconName;
    }
    // Default icons by type
    switch (connection.providerType) {
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

  /**
   * Get icon container classes for a provider type.
   */
  getProviderIconClasses(type: OAuthProviderType): string {
    const baseClasses = 'flex size-12 shrink-0 items-center justify-center rounded-sm';
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

  /**
   * Format a date string for display.
   */
  formatDate(dateString: string): string {
    try {
      const date = new Date(dateString);
      return date.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      });
    } catch {
      return dateString;
    }
  }
}
