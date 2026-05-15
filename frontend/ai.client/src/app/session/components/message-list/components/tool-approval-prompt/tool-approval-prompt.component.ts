import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroChevronRight,
  heroCheck,
  heroCommandLine,
  heroShieldCheck,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import {
  ToolApprovalDecision,
  ToolApprovalRequest,
  ToolApprovalService,
} from '../../../../../services/tool-approval/tool-approval.service';

/**
 * Inline tool-approval prompt rendered alongside the assistant message
 * whose tool call needs user approval. Visual language mirrors
 * `OAuthConsentPromptComponent`: a compact horizontal pill with a 2px
 * primary-500 left accent, the shared `.action-btn` for the affirmative
 * action, and the same lift-on-mount animation. Adds an optional
 * args-inspection footer when the tool was invoked with arguments.
 */
@Component({
  selector: 'app-tool-approval-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroChevronRight,
      heroCheck,
      heroCommandLine,
      heroShieldCheck,
      heroXMark,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div
      class="approval-prompt group relative max-w-xl overflow-hidden rounded-lg border border-gray-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-white/10 dark:bg-slate-800/70"
      role="region"
      aria-live="polite"
      [attr.aria-label]="'Approval required for tool ' + request().toolName"
    >
      <span
        class="absolute inset-y-0 left-0 w-[2px] bg-primary-500 dark:bg-primary-400"
        aria-hidden="true"
      ></span>

      <!-- Main row: matches the OAuth pill layout. -->
      <div class="flex items-center gap-2.5 py-1.5 pr-1.5 pl-3">
        <div
          class="flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-gray-50 ring-1 ring-gray-200/70 dark:bg-slate-900 dark:ring-white/10"
        >
          <ng-icon
            name="heroCommandLine"
            class="size-5 text-gray-700 dark:text-gray-300"
            aria-hidden="true"
          />
        </div>

        <div class="min-w-0 flex-1">
          <p
            class="inline-flex items-center gap-1 text-[10px] leading-none font-semibold uppercase tracking-[0.08em] text-primary-600 dark:text-primary-300"
          >
            <ng-icon name="heroShieldCheck" class="size-3" aria-hidden="true" />
            Approval needed
          </p>
          <p class="text-xs/5 text-gray-900 dark:text-gray-100">
            Approve
            <code
              class="rounded bg-gray-100 px-1 py-0.5 font-mono text-[11px] font-semibold text-gray-900 dark:bg-white/10 dark:text-gray-100"
              >{{ request().toolName }}</code
            >
            to let the assistant continue.
          </p>
        </div>

        <div class="flex shrink-0 items-center gap-1">
          <button
            type="button"
            (click)="resolve('declined')"
            class="decline-btn"
            [disabled]="resolving()"
            aria-label="Decline tool call"
          >
            <ng-icon name="heroXMark" class="size-3" aria-hidden="true" />
            <span>Decline</span>
          </button>
          <button
            type="button"
            (click)="resolve('approved')"
            class="action-btn"
            [disabled]="resolving()"
            aria-label="Approve tool call"
          >
            @if (resolving()) {
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
              <span>Working…</span>
            } @else {
              <ng-icon name="heroCheck" class="size-3" aria-hidden="true" />
              <span>Approve</span>
            }
          </button>
        </div>
      </div>

      <!-- Args inspector: shown only when the tool was called with input. -->
      @if (hasInput()) {
        <div
          class="border-t border-gray-200/80 px-3 py-1.5 dark:border-white/10"
        >
          <button
            type="button"
            (click)="toggleArgs()"
            [attr.aria-expanded]="argsExpanded()"
            class="inline-flex items-center gap-1 rounded text-[11px] font-medium text-gray-500 transition-colors hover:text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500 dark:text-gray-400 dark:hover:text-gray-100"
          >
            <ng-icon
              name="heroChevronRight"
              class="size-3 transition-transform duration-150"
              [class.rotate-90]="argsExpanded()"
              aria-hidden="true"
            />
            <span>{{ argsExpanded() ? 'Hide arguments' : 'View arguments' }}</span>
          </button>
          @if (argsExpanded()) {
            <pre
              class="args-fold mt-1.5 max-h-72 overflow-auto rounded-md border border-gray-200/80 bg-gray-50 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-gray-800 dark:border-white/10 dark:bg-slate-900/60 dark:text-gray-200"
            >{{ formattedInput() }}</pre>
          }
        </div>
      }
    </div>
  `,
  styles: `
    @import 'tailwindcss';
    @custom-variant dark (&:where(.dark, .dark *));

    :host {
      display: block;
    }

    .approval-prompt {
      animation: approval-rise 0.32s cubic-bezier(0.16, 1, 0.3, 1);
    }

    /* Override the global \`.message-block p\` rule (styles.css) which adds
       a 16px margin-bottom for prose paragraphs. Inside the prompt the two
       <p>s are a tight label + description pair. */
    .approval-prompt p {
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

    .decline-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      border-radius: 0.375rem;
      padding: 0.25rem 0.5rem;
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--color-gray-600);
      background: transparent;
      transition:
        background-color 120ms ease,
        color 120ms ease;
    }

    .decline-btn:hover:not(:disabled) {
      background: var(--color-gray-100);
      color: var(--color-gray-900);
    }

    .decline-btn:focus-visible {
      outline: 2px solid var(--color-gray-400);
      outline-offset: 2px;
    }

    .decline-btn:disabled {
      opacity: 0.5;
      cursor: default;
    }

    :where(.dark, .dark *) .decline-btn {
      color: var(--color-gray-300);
    }

    :where(.dark, .dark *) .decline-btn:hover:not(:disabled) {
      background: rgb(255 255 255 / 0.08);
      color: white;
    }

    .args-fold {
      animation: args-fold 0.2s ease-out;
    }

    @keyframes approval-rise {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @keyframes args-fold {
      from {
        opacity: 0;
        transform: translateY(-2px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .approval-prompt,
      .args-fold {
        animation: none;
      }
      .action-btn,
      .decline-btn {
        transition: none;
      }
    }
  `,
})
export class ToolApprovalPromptComponent {
  request = input.required<ToolApprovalRequest>();

  private approvalService = inject(ToolApprovalService);

  protected argsExpanded = signal(false);
  protected resolving = signal(false);

  protected hasInput = computed<boolean>(() => {
    const value = this.request().toolInput;
    return typeof value === 'string' && value.trim().length > 0;
  });

  protected formattedInput = computed<string>(() => this.request().toolInput ?? '');

  toggleArgs(): void {
    this.argsExpanded.update((v) => !v);
  }

  async resolve(decision: ToolApprovalDecision): Promise<void> {
    if (this.resolving()) return;
    this.resolving.set(true);
    try {
      await this.approvalService.resolve(this.request().interruptId, decision);
    } finally {
      this.resolving.set(false);
    }
  }
}
