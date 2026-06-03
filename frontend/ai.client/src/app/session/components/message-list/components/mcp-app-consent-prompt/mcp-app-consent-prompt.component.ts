import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowTopRightOnSquare,
  heroCheck,
  heroShieldCheck,
  heroVideoCamera,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import {
  McpAppConsentService,
  PendingConsent,
} from '../../../../services/mcp-apps/mcp-app-consent.service';

/**
 * Inline consent prompt for an App-initiated action (MCP Apps PR #6).
 *
 * Frontend-only: the request came from a postMessage on the embedded App,
 * not a backend turn, so this is purely a client gate (see
 * {@link McpAppConsentService}). Visually mirrors the OAuth consent prompt so
 * the two read as one family; the App frame renders it inside the frame
 * (below the title bar, above the iframe) so it stays anchored to the App and
 * remains visible — and answerable — when the frame is fullscreen.
 */
@Component({
  selector: 'app-mcp-app-consent-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroArrowTopRightOnSquare,
      heroCheck,
      heroShieldCheck,
      heroVideoCamera,
      heroXMark,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div
      class="mcp-consent group relative flex max-w-xl items-center gap-2.5 overflow-hidden rounded-lg border border-gray-200/80 bg-white py-1.5 pr-1.5 pl-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-white/10 dark:bg-slate-800/70"
      role="region"
      aria-live="polite"
      [attr.aria-label]="ariaLabel()"
    >
      <span
        class="absolute inset-y-0 left-0 w-[2px] bg-primary-500 dark:bg-primary-400"
        aria-hidden="true"
      ></span>

      <div
        class="flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-gray-50 ring-1 ring-gray-200/70 dark:bg-slate-900 dark:ring-white/10"
      >
        <ng-icon
          [name]="isLink() ? 'heroArrowTopRightOnSquare' : 'heroVideoCamera'"
          class="size-5 text-gray-700 dark:text-gray-300"
          aria-hidden="true"
        />
      </div>

      <div class="min-w-0 flex-1">
        <p
          class="inline-flex items-center gap-1 text-[10px] leading-none font-semibold uppercase tracking-[0.08em] text-primary-600 dark:text-primary-300"
        >
          <ng-icon name="heroShieldCheck" class="size-3" aria-hidden="true" />
          Permission requested
        </p>
        <p class="truncate text-xs/5 text-gray-900 dark:text-gray-100">
          @if (isLink()) {
            This app wants to open
            <span class="font-semibold">{{ linkHost() }}</span>
          } @else {
            This app requests
            <span class="font-semibold">{{ capabilityList() }}</span>
          }
        </p>
      </div>

      <div class="flex shrink-0 items-center gap-1">
        <button
          type="button"
          (click)="allow()"
          class="action-btn"
          [attr.aria-label]="'Allow: ' + summary()"
        >
          <ng-icon name="heroCheck" class="size-3" aria-hidden="true" />
          <span>Allow</span>
        </button>
        <button
          type="button"
          (click)="deny()"
          class="dismiss-btn flex size-6 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
          [attr.aria-label]="'Deny: ' + summary()"
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

    .mcp-consent {
      animation: mcp-consent-rise 0.32s cubic-bezier(0.16, 1, 0.3, 1);
    }

    .mcp-consent p {
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

    .action-btn:hover {
      background: var(--color-secondary-600);
    }

    .action-btn:active {
      transform: translateY(1px);
    }

    .action-btn:focus-visible {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    .dismiss-btn {
      opacity: 0;
    }

    .group:hover .dismiss-btn,
    .group:focus-within .dismiss-btn {
      opacity: 1;
    }

    @keyframes mcp-consent-rise {
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
      .mcp-consent {
        animation: none;
      }
      .action-btn {
        transition: none;
      }
    }
  `,
})
export class McpAppConsentPromptComponent {
  readonly prompt = input.required<PendingConsent>();

  private readonly consentService = inject(McpAppConsentService);

  protected readonly isLink = computed(
    () => this.prompt().request.kind === 'open-link',
  );

  protected readonly linkHost = computed<string>(() => {
    const req = this.prompt().request;
    if (req.kind !== 'open-link') return '';
    try {
      return new URL(req.url).host;
    } catch {
      return req.url;
    }
  });

  protected readonly capabilityList = computed<string>(() => {
    const req = this.prompt().request;
    if (req.kind !== 'capabilities') return '';
    const labels: Record<string, string> = {
      camera: 'camera',
      microphone: 'microphone',
      geolocation: 'location',
      clipboardWrite: 'clipboard',
    };
    return req.capabilities.map((c) => labels[c] ?? c).join(', ');
  });

  protected readonly summary = computed<string>(() =>
    this.isLink()
      ? `open ${this.linkHost()}`
      : `${this.capabilityList()} access`,
  );

  protected readonly ariaLabel = computed<string>(
    () => `App permission requested: ${this.summary()}`,
  );

  allow(): void {
    this.consentService.answer(this.prompt().id, true);
  }

  deny(): void {
    this.consentService.answer(this.prompt().id, false);
  }
}
