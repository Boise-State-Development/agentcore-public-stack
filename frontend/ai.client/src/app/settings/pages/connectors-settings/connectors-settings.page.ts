import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  effect,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroLink,
  heroCloud,
  heroCodeBracket,
  heroAcademicCap,
  heroCheckCircle,
  heroArrowPath,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { UserConnectorsService } from '../../connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { UserConnector } from '../../connectors/models/user-connector.model';
import { ToastService } from '../../../services/toast/toast.service';

type ConnectState = 'idle' | 'initiating' | 'awaiting' | 'connected' | 'error';

@Component({
  selector: 'app-connectors-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroLink,
      heroCloud,
      heroCodeBracket,
      heroAcademicCap,
      heroCheckCircle,
      heroArrowPath,
      heroExclamationTriangle,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div class="flex flex-col gap-8">
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Connectors</h2>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Connect your third-party accounts so agents can call tools on your behalf.
        </p>
      </div>

      @if (resource.isLoading()) {
        <div class="flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
          <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600 dark:border-gray-600"></div>
          Loading connectors...
        </div>
      } @else if (resource.error()) {
        <div class="flex items-start gap-3 rounded-sm border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20">
          <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-red-600 dark:text-red-400" />
          <div>
            <h3 class="text-sm/6 font-medium text-red-800 dark:text-red-200">Couldn't load connectors</h3>
            <p class="mt-1 text-sm/6 text-red-700 dark:text-red-300">
              {{ resource.error()?.message || 'Try again in a moment.' }}
            </p>
            <button
              type="button"
              (click)="resource.reload()"
              class="mt-2 text-sm/6 font-medium text-red-700 underline hover:text-red-800 dark:text-red-200"
            >
              Retry
            </button>
          </div>
        </div>
      } @else if (connectors().length === 0) {
        <div class="rounded-sm border border-dashed border-gray-300 p-8 text-center dark:border-gray-700">
          <ng-icon name="heroLink" class="mx-auto size-8 text-gray-400" />
          <p class="mt-3 text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            No connectors are available to you yet.
          </p>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            Ask an administrator to enable a connector for your role.
          </p>
        </div>
      } @else {
        <ul class="flex flex-col gap-3">
          @for (connector of connectors(); track connector.providerId) {
            <li
              class="flex items-start justify-between gap-4 rounded-sm border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800"
            >
              <div class="flex items-start gap-3">
                <div [class]="iconClasses(connector.providerType)">
                  <ng-icon [name]="connector.iconName || defaultIcon(connector.providerType)" class="size-5" />
                </div>
                <div>
                  <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                    {{ connector.displayName }}
                  </h3>
                  @if (connector.scopes.length > 0) {
                    <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">
                      Requests: {{ connector.scopes.join(', ') }}
                    </p>
                  }
                </div>
              </div>

              @let state = getState(connector.providerId);
              <div class="flex shrink-0 items-center gap-2">
                @if (state === 'connected') {
                  <span class="inline-flex items-center gap-1.5 rounded-sm bg-emerald-50 px-2.5 py-1.5 text-xs/5 font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
                    <ng-icon name="heroCheckCircle" class="size-4" />
                    Connected
                  </span>
                } @else if (state === 'error') {
                  <span class="inline-flex items-center gap-1.5 rounded-sm bg-red-50 px-2.5 py-1.5 text-xs/5 font-medium text-red-700 dark:bg-red-900/30 dark:text-red-300">
                    <ng-icon name="heroExclamationTriangle" class="size-4" />
                    Failed
                  </span>
                }

                <button
                  type="button"
                  (click)="connect(connector.providerId)"
                  [disabled]="state === 'initiating' || state === 'awaiting'"
                  class="inline-flex items-center gap-1.5 rounded-sm bg-blue-600 px-3 py-1.5 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  @if (state === 'initiating') {
                    <div class="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white"></div>
                    Starting...
                  } @else if (state === 'awaiting') {
                    <ng-icon name="heroArrowPath" class="size-4" />
                    Awaiting consent
                  } @else if (state === 'connected') {
                    Reconnect
                  } @else {
                    Connect
                  }
                </button>
              </div>
            </li>
          }
        </ul>
      }
    </div>
  `,
})
export class ConnectorsSettingsPage {
  private readonly connectorsService = inject(UserConnectorsService);
  private readonly consentService = inject(OAuthConsentService);
  private readonly toast = inject(ToastService);

  protected readonly resource = this.connectorsService.connectorsResource;

  protected readonly connectors = computed<UserConnector[]>(
    () => this.resource.value() ?? [],
  );

  private readonly states = signal<Map<string, ConnectState>>(new Map());

  constructor() {
    // Flip a provider to `connected` when the /oauth-complete landing page
    // postMessages success. This is the same signal the chat-input banner
    // listens to, so both UIs stay in sync.
    effect(() => {
      const completion = this.consentService.completion();
      if (!completion || !completion.providerId) return;
      if (completion.status === 'success') {
        this.setState(completion.providerId, 'connected');
      } else {
        this.setState(completion.providerId, 'error');
      }
      this.consentService.acknowledgeCompletion();
    });

    // Probe AgentCore on load (and whenever the connector list changes)
    // to restore the "Connected" badge without the user having to click.
    // We call initiateConsent just for its `connected` flag — if it returns
    // false we discard the URL, the user can still click Connect manually.
    effect(() => {
      const connectors = this.connectors();
      if (connectors.length === 0) return;
      void this.probeConnectedStatus(connectors);
    });
  }

  private async probeConnectedStatus(connectors: UserConnector[]): Promise<void> {
    const unknown = connectors.filter((c) => !this.states().has(c.providerId));
    await Promise.all(
      unknown.map(async (c) => {
        try {
          const result = await this.connectorsService.initiateConsent(c.providerId);
          if (result.connected && this.getState(c.providerId) === 'idle') {
            this.setState(c.providerId, 'connected');
          }
        } catch {
          // Leave state as idle — user can still click Connect to retry.
        }
      }),
    );
  }

  protected getState(providerId: string): ConnectState {
    return this.states().get(providerId) ?? 'idle';
  }

  private setState(providerId: string, state: ConnectState): void {
    this.states.update((map) => {
      const next = new Map(map);
      next.set(providerId, state);
      return next;
    });
  }

  protected async connect(providerId: string): Promise<void> {
    this.setState(providerId, 'initiating');
    try {
      const result = await this.connectorsService.initiateConsent(providerId);
      if (result.connected) {
        this.setState(providerId, 'connected');
        this.toast.success(`${this.displayNameFor(providerId)} is already connected.`);
        return;
      }
      if (!result.authorizationUrl) {
        this.setState(providerId, 'error');
        this.toast.error('Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(providerId, result.authorizationUrl);
      this.consentService.openConsentPopup(providerId);
      this.setState(providerId, 'awaiting');
    } catch (err: unknown) {
      console.error('Consent initiation failed', err);
      this.setState(providerId, 'error');
      const detail = (err as { error?: { detail?: string }; message?: string })?.error?.detail;
      this.toast.error(detail ?? 'Could not start the consent flow.');
    }
  }

  private displayNameFor(providerId: string): string {
    return this.connectors().find((c) => c.providerId === providerId)?.displayName ?? providerId;
  }

  protected defaultIcon(providerType: UserConnector['providerType']): string {
    switch (providerType) {
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

  protected iconClasses(providerType: UserConnector['providerType']): string {
    const base = 'flex size-10 items-center justify-center rounded-sm';
    switch (providerType) {
      case 'google':
        return `${base} bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400`;
      case 'microsoft':
        return `${base} bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400`;
      case 'github':
        return `${base} bg-gray-800 text-white dark:bg-gray-600`;
      case 'canvas':
        return `${base} bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400`;
      default:
        return `${base} bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400`;
    }
  }
}
