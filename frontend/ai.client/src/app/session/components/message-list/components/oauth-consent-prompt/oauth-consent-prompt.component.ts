import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroAcademicCap,
  heroArrowTopRightOnSquare,
  heroCloud,
  heroCodeBracket,
  heroLink,
  heroLockClosed,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import {
  OAuthConsentRequest,
  OAuthConsentService,
} from '../../../../../services/oauth-consent/oauth-consent.service';
import { UserConnector } from '../../../../../settings/connectors/models/user-connector.model';
import { UserConnectorsService } from '../../../../../settings/connectors/services/user-connectors.service';

/**
 * Inline OAuth consent prompt rendered alongside the assistant message whose
 * tool call needed authorization. Looks up the connector definition to display
 * its icon (admin-uploaded base64 wins over heroicon name) and friendly name,
 * delegates click handling to {@link OAuthConsentService}.
 */
@Component({
  selector: 'app-oauth-consent-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroAcademicCap,
      heroArrowTopRightOnSquare,
      heroCloud,
      heroCodeBracket,
      heroLink,
      heroLockClosed,
      heroXMark,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div
      class="oauth-prompt group relative flex max-w-xl items-center gap-2.5 overflow-hidden rounded-lg border border-gray-200/80 bg-white py-1.5 pr-1.5 pl-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-white/10 dark:bg-slate-800/70"
      role="region"
      aria-live="polite"
      [attr.aria-label]="'Authorization required for ' + displayName()"
    >
      <span
        class="absolute inset-y-0 left-0 w-[2px] bg-primary-500 dark:bg-primary-400"
        aria-hidden="true"
      ></span>

      <div
        class="flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-gray-50 ring-1 ring-gray-200/70 dark:bg-slate-900 dark:ring-white/10"
      >
        @if (iconDataUrl(); as data) {
          <img
            [src]="data"
            [alt]="displayName() + ' icon'"
            class="size-full object-contain"
          />
        } @else {
          <ng-icon
            [name]="iconName()"
            class="size-5 text-gray-700 dark:text-gray-300"
            aria-hidden="true"
          />
        }
      </div>

      <div class="min-w-0 flex-1">
        <p
          class="inline-flex items-center gap-1 text-[10px] leading-none font-semibold uppercase tracking-[0.08em] text-primary-600 dark:text-primary-300"
        >
          <ng-icon name="heroLockClosed" class="size-3" aria-hidden="true" />
          Authorization needed
        </p>
        <p class="text-xs/5 text-gray-900 dark:text-gray-100">
          @if (isBlocked()) {
            Popup blocked. Open
            <span class="font-semibold">{{ displayName() }}</span>
            in a new tab to continue.
          } @else {
            Connect
            <span class="font-semibold">{{ displayName() }}</span>
            so the assistant can finish this request.
          }
        </p>
      </div>

      <div class="flex shrink-0 items-center gap-1">
        @if (isBlocked() && request().authorizationUrl; as blockedUrl) {
          <a
            [href]="blockedUrl"
            target="_blank"
            rel="noopener noreferrer"
            class="action-btn"
            [attr.aria-label]="'Open ' + displayName() + ' authorization in a new tab'"
          >
            <span>Open</span>
            <ng-icon name="heroArrowTopRightOnSquare" class="ml-1 size-3" aria-hidden="true" />
          </a>
        } @else {
          <button
            type="button"
            (click)="connect()"
            class="action-btn"
            [disabled]="isInFlight()"
            [attr.aria-label]="'Connect to ' + displayName()"
          >
            @if (isInFlight()) {
              <svg
                class="size-3 animate-spin"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <circle
                  class="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  stroke-width="4"
                ></circle>
                <path
                  class="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                ></path>
              </svg>
              <span>Waiting…</span>
            } @else {
              <span>Connect</span>
              <ng-icon
                name="heroArrowTopRightOnSquare"
                class="size-3"
                aria-hidden="true"
              />
            }
          </button>
        }
        <button
          type="button"
          (click)="dismiss($event)"
          class="dismiss-btn flex size-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
          aria-label="Dismiss authorization prompt"
        >
          <ng-icon name="heroXMark" class="size-3.5" aria-hidden="true" />
        </button>
      </div>
    </div>
  `,
  styles: `
    @import 'tailwindcss';
    @custom-variant dark (&:where(.dark, .dark *));

    :host {
      display: block;
    }

    .oauth-prompt {
      animation: oauth-rise 0.32s cubic-bezier(0.16, 1, 0.3, 1);
    }

    /* Override the global \`.message-block p\` rule (styles.css) which adds
       a 16px margin-bottom for prose paragraphs. Inside the prompt the two
       <p>s are a tight label + description pair. */
    .oauth-prompt p {
      margin-bottom: 0;
    }

    .action-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      border-radius: 0.375rem;
      padding: 0.25rem 0.625rem;
      font-size: 0.75rem;
      font-weight: 600;
      color: white;
      background: var(--color-secondary-500);
      transition:
        background-color 120ms ease,
        transform 120ms ease;
    }

    .action-btn:hover:not(:disabled) {
      background: var(--color-secondary-600);
    }

    .action-btn:active:not(:disabled) {
      transform: translateY(1px);
    }

    .action-btn:focus-visible {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    .action-btn:disabled {
      opacity: 0.85;
      cursor: default;
    }

    /* Dismiss is subtle until the row is hovered or focused. */
    .dismiss-btn {
      opacity: 0;
    }

    .group:hover .dismiss-btn,
    .group:focus-within .dismiss-btn {
      opacity: 1;
    }

    @keyframes oauth-rise {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .oauth-prompt {
        animation: none;
      }
      .action-btn {
        transition: none;
      }
    }
  `,
})
export class OAuthConsentPromptComponent {
  request = input.required<OAuthConsentRequest>();

  protected consentService = inject(OAuthConsentService);
  private connectorsService = inject(UserConnectorsService);

  /** Connector definition for this providerId, when the catalog is loaded. */
  private connector = computed<UserConnector | null>(() => {
    const connectors = this.connectorsService.connectorsResource.value();
    if (!connectors) return null;
    return connectors.find((c) => c.providerId === this.request().providerId) ?? null;
  });

  /** Admin-uploaded base64 icon. Wins over heroicon when present. */
  protected iconDataUrl = computed<string | null>(() => this.connector()?.iconData ?? null);

  /** Heroicon fallback when no iconData exists. Mirrors the cascade used by
   *  the connectors-settings page so the same connector renders identically. */
  protected iconName = computed<string>(() => {
    const c = this.connector();
    if (c?.iconName) return c.iconName;
    switch (c?.providerType) {
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
  });

  protected displayName = computed<string>(() => {
    const c = this.connector();
    if (c?.displayName) return c.displayName;
    return this.titleCase(this.request().providerId);
  });

  protected isInFlight = computed<boolean>(() =>
    this.consentService.isInFlight(this.request().providerId),
  );

  protected isBlocked = computed<boolean>(() =>
    this.consentService.isBlocked(this.request().providerId),
  );

  connect(): void {
    void this.consentService.openConsentPopup(this.request().providerId);
  }

  dismiss(event: Event): void {
    event.stopPropagation();
    this.consentService.dismiss(this.request().providerId);
  }

  private titleCase(providerId: string): string {
    if (!providerId) return 'This tool';
    return providerId
      .replace(/[-_]+/g, ' ')
      .split(' ')
      .filter((part) => part.length > 0)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  }
}
