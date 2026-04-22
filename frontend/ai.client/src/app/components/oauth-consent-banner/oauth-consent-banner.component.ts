import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroLink, heroArrowTopRightOnSquare, heroXMark } from '@ng-icons/heroicons/outline';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';

/**
 * Renders a compact "Connect to X" prompt above the chat input whenever the
 * SSE stream surfaces an `oauth_required` event. Clicking the button opens
 * AgentCore Identity's consent URL in a popup; {@link OAuthConsentService}
 * listens for the completion postMessage and clears the entry automatically.
 */
@Component({
  selector: 'app-oauth-consent-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroLink, heroArrowTopRightOnSquare, heroXMark })],
  template: `
    @if (consentService.hasPending()) {
      <div class="flex flex-col items-center gap-1.5 mb-1.5" role="region" aria-label="External tool authorization">
        @for (request of consentService.pending(); track request.providerId) {
          <div
            class="flex items-center gap-2 px-3 py-1.5 text-xs rounded-lg border border-primary-300 bg-primary-50 text-primary-800 dark:border-primary-500 dark:bg-primary-900/30 dark:text-primary-200 animate-fade-in"
            role="status"
            aria-live="polite"
          >
            <ng-icon name="heroLink" class="size-3.5 shrink-0" aria-hidden="true" />
            <span class="font-medium">
              {{ labelFor(request.providerId) }} needs your authorization.
            </span>
            <button
              type="button"
              (click)="connect(request.providerId)"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-primary-600 text-white font-medium hover:bg-primary-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 transition-colors"
              [attr.aria-label]="'Connect to ' + labelFor(request.providerId)"
            >
              @if (consentService.isInFlight(request.providerId)) {
                <svg class="size-3 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                </svg>
                <span>Waiting…</span>
              } @else {
                <ng-icon name="heroArrowTopRightOnSquare" class="size-3" aria-hidden="true" />
                <span>Connect</span>
              }
            </button>
            <button
              type="button"
              (click)="dismiss(request.providerId, $event)"
              class="p-0.5 -mr-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
              aria-label="Dismiss"
            >
              <ng-icon name="heroXMark" class="size-3" aria-hidden="true" />
            </button>
          </div>
        }
      </div>
    }
  `,
  styles: [`
    @keyframes fadeIn {
      from {
        opacity: 0;
        transform: translateY(4px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    .animate-fade-in {
      animation: fadeIn 0.15s ease-out;
    }
  `],
})
export class OAuthConsentBannerComponent {
  protected consentService = inject(OAuthConsentService);

  labelFor(providerId: string): string {
    if (!providerId) {
      return 'This tool';
    }
    return providerId
      .replace(/[-_]+/g, ' ')
      .split(' ')
      .filter((part) => part.length > 0)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  }

  connect(providerId: string): void {
    this.consentService.openConsentPopup(providerId);
  }

  dismiss(providerId: string, event: Event): void {
    event.stopPropagation();
    this.consentService.dismiss(providerId);
  }
}
