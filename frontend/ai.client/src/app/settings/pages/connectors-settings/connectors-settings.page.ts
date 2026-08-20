import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  effect,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
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
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

type ConnectState =
  | 'probing'
  | 'idle'
  | 'initiating'
  | 'awaiting'
  | 'connected'
  | 'error';

@Component({
  selector: 'app-connectors-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, SpinnerComponent],
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
          <app-spinner size="sm" label="Loading connectors" />
          Loading connectors...
        </div>
      } @else if (resource.error()) {
        <div class="flex items-start gap-3 rounded-sm border border-state-danger-200 bg-state-danger-50 p-4 dark:border-state-danger-800 dark:bg-state-danger-900/20">
          <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-state-danger-600 dark:text-state-danger-400" />
          <div>
            <h3 class="text-sm/6 font-medium text-state-danger-800 dark:text-state-danger-200">Couldn't load connectors</h3>
            <p class="mt-1 text-sm/6 text-state-danger-700 dark:text-state-danger-300">
              {{ resource.error()?.message || 'Try again in a moment.' }}
            </p>
            <button
              type="button"
              (click)="resource.reload()"
              class="mt-2 text-sm/6 font-medium text-state-danger-700 underline hover:text-state-danger-800 dark:text-state-danger-200"
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
              class="flex items-center justify-between gap-4 rounded-sm border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800"
            >
              <div class="flex items-center gap-3">
                @if (connector.iconData) {
                  <div class="flex size-10 shrink-0 items-center justify-center rounded-md bg-gray-50 dark:bg-gray-900">
                    <img
                      [src]="connector.iconData"
                      [alt]="connector.displayName + ' icon'"
                      class="size-8 object-contain"
                    />
                  </div>
                } @else {
                  <div [class]="iconClasses(connector.providerType)">
                    <ng-icon [name]="connector.iconName || defaultIcon(connector.providerType)" class="size-5" />
                  </div>
                }
                <div>
                  <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                    {{ connector.displayName }}
                  </h3>
                </div>
              </div>

              @let state = getState(connector.providerId);
              @if (state === 'probing') {
                <!-- Skeleton placeholder while the side-effect-free /status
                     probe resolves. Sized to roughly match the badge + button
                     footprint so the row doesn't reflow when the real UI
                     swaps in. Decorative — list-level loading is announced
                     by the resource state above. -->
                <div class="flex shrink-0 items-center gap-2" aria-hidden="true">
                  <div class="h-7 w-24 animate-pulse rounded-sm bg-gray-200 dark:bg-gray-700"></div>
                  <div class="h-7 w-20 animate-pulse rounded-sm bg-gray-200 dark:bg-gray-700"></div>
                </div>
              } @else {
                <div class="flex shrink-0 items-center gap-2">
                  @if (state === 'connected') {
                    <span class="inline-flex items-center gap-1.5 rounded-sm bg-state-success-50 px-2.5 py-1.5 text-xs/5 font-medium text-state-success-700 dark:bg-state-success-900/30 dark:text-state-success-300">
                      <ng-icon name="heroCheckCircle" class="size-4" />
                      Connected
                    </span>
                  } @else if (state === 'error') {
                    <span class="inline-flex items-center gap-1.5 rounded-sm bg-state-danger-50 px-2.5 py-1.5 text-xs/5 font-medium text-state-danger-700 dark:bg-state-danger-900/30 dark:text-state-danger-300">
                      <ng-icon name="heroExclamationTriangle" class="size-4" />
                      Failed
                    </span>
                  }

                  @if (state === 'connected') {
                    <button
                      type="button"
                      (click)="disconnect(connector)"
                      class="inline-flex items-center gap-1.5 rounded-sm border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-semibold text-gray-700 shadow-xs hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-gray-300/50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      Disconnect
                    </button>
                  } @else {
                    <!--
                      Brand accent. Uses the generated \`primary-accessible\`
                      alias rather than a fixed step, so the white label text
                      clears WCAG AA whatever Brand_Color is configured. No
                      \`dark:\` override: the contrast that matters for a solid
                      fill is fill-vs-white-text, which is identical in both
                      themes, so lightening in dark mode would only erode it.
                      Hover is a brightness filter rather than a darker step
                      because for a light brand color the accessible variant
                      can already be darker than step 700, which would make
                      \`hover:bg-primary-700\` brighten on hover and lose the
                      guarantee.
                    -->
                    <button
                      type="button"
                      (click)="connect(connector.providerId)"
                      [disabled]="state === 'initiating' || state === 'awaiting'"
                      class="inline-flex items-center gap-1.5 rounded-sm bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white shadow-xs transition-[filter] hover:brightness-95 focus:outline-hidden focus:ring-3 focus:ring-primary-accessible/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      @if (state === 'initiating') {
                        <app-spinner size="sm" variant="on-solid" label="Starting" />
                        Starting...
                      } @else if (state === 'awaiting') {
                        <ng-icon name="heroArrowPath" class="size-4" />
                        Awaiting consent
                      } @else {
                        Connect
                      }
                    </button>
                  }
                </div>
              }
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
  private readonly dialog = inject(Dialog);

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
    // Uses the side-effect-free `/status` endpoint — `initiateConsent`
    // would record a pending session on the server every time the vault is
    // empty, which is wasteful when we only want a badge.
    effect(() => {
      const connectors = this.connectors();
      if (connectors.length === 0) return;
      void this.probeConnectedStatus(connectors);
    });

    // If a user closes the OAuth popup without completing consent, the
    // service drops the providerId from inFlight. Reset our local state
    // back to `idle` so the Connect button becomes interactive again.
    effect(() => {
      const inFlight = this.consentService.inFlightProviders();
      this.states.update((states) => {
        let changed = false;
        const next = new Map(states);
        for (const [providerId, state] of next.entries()) {
          if (state === 'awaiting' && !inFlight.has(providerId)) {
            next.set(providerId, 'idle');
            changed = true;
          }
        }
        return changed ? next : states;
      });
    });
  }

  private async probeConnectedStatus(connectors: UserConnector[]): Promise<void> {
    const unknown = connectors.filter((c) => !this.states().has(c.providerId));
    if (unknown.length === 0) return;

    // Flip to `probing` synchronously so the skeleton renders before the
    // first network round-trip resolves, instead of flashing the Connect
    // button and replacing it half a second later.
    unknown.forEach((c) => this.setState(c.providerId, 'probing'));

    await Promise.all(
      unknown.map(async (c) => {
        try {
          const status = await this.connectorsService.getStatus(c.providerId);
          // Only resolve from `probing` — if the user clicked Connect
          // mid-probe we don't want to clobber their in-flight state.
          if (this.getState(c.providerId) === 'probing') {
            this.setState(c.providerId, status.connected ? 'connected' : 'idle');
          }
        } catch {
          // Status check failed (e.g. backend 503). Fall back to idle so the
          // Connect button is interactive and the user can retry manually.
          if (this.getState(c.providerId) === 'probing') {
            this.setState(c.providerId, 'idle');
          }
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

  protected async disconnect(connector: UserConnector): Promise<void> {
    // The "destructive" styling matches the existing pattern for delete
    // affordances in this codebase (see file-browser bulk delete). The
    // message flags that the upstream provider may still hold an
    // authorization the user should revoke separately for full removal.
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: `Disconnect ${connector.displayName}`,
        message:
          `Agents will stop using this connector for you, and you'll be ` +
          `prompted to re-authorize the next time it's needed. For full ` +
          `revocation (e.g. removing this app from your Google account), ` +
          `visit your account settings at the provider.`,
        confirmText: 'Disconnect',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed !== true) return;

    try {
      await this.connectorsService.disconnect(connector.providerId);
      this.setState(connector.providerId, 'idle');
      this.toast.success(`${connector.displayName} disconnected.`);
    } catch (err: unknown) {
      console.error('Disconnect failed', err);
      const detail = (err as { error?: { detail?: string }; message?: string })?.error?.detail;
      this.toast.error(detail ?? 'Could not disconnect.');
    }
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

  /**
   * Icon container styling per connector provider.
   *
   * Uses the `vendor-*` identity tokens (src/styles/tokens/identity.css)
   * rather than raw palette utilities: these hues track each third party's
   * own brand, so they must NOT follow Brand_Config. GitHub stays on
   * neutrals because its brand is near-black, and neutrals are outside the
   * rebrandable surface.
   */
  protected iconClasses(providerType: UserConnector['providerType']): string {
    const base = 'flex size-10 items-center justify-center rounded-sm';
    switch (providerType) {
      case 'google':
        return `${base} bg-vendor-google-100 text-vendor-google-600 dark:bg-vendor-google-900/30 dark:text-vendor-google-400`;
      case 'microsoft':
        return `${base} bg-vendor-microsoft-100 text-vendor-microsoft-600 dark:bg-vendor-microsoft-900/30 dark:text-vendor-microsoft-400`;
      case 'github':
        return `${base} bg-gray-800 text-white dark:bg-gray-600`;
      case 'canvas':
        return `${base} bg-vendor-canvas-100 text-vendor-canvas-600 dark:bg-vendor-canvas-900/30 dark:text-vendor-canvas-400`;
      default:
        return `${base} bg-vendor-generic-100 text-vendor-generic-600 dark:bg-vendor-generic-900/30 dark:text-vendor-generic-400`;
    }
  }
}
