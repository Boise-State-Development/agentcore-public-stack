import { Component, ChangeDetectionStrategy, inject, computed } from '@angular/core';
import { QuotaWarningService } from '../../services/quota/quota-warning.service';
import { SessionService } from '../../session/services/session/session.service';
import { FEATURES } from '../../services/features';
import { QuotaRequestActionComponent } from '../quota-request/quota-request-action.component';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroBanknotes,
  heroChatBubbleLeftRight,
  heroExclamationTriangle,
  heroNoSymbol,
  heroXMark,
} from '@ng-icons/heroicons/outline';

/**
 * Usage at which the warning tab offers "Request more": the tier's default soft
 * limit (quota-requests spec §5.1). The 50% and 75% rungs below it are
 * information, not a prompt to spend more. The `quota_warning` event does not
 * carry the tier's own soft limit, so a tier that moves it still prompts at 80%.
 */
export const REQUEST_PROMPT_PERCENT = 80;

/**
 * Subtle quota warning indicator component
 *
 * Displays a compact warning message above the chat input when the user
 * approaches their usage quota. Rungs are tier-configurable and now start
 * at 50% (see the quota runway in
 * docs/specs/compaction-over-threshold-cache-spiral.md §3 PR-5).
 *
 * Also renders the **per-session** notice — "this conversation has used $X
 * of your $Y" — which answers a different question than the per-user
 * warning and is dismissible on its own. It is scoped to the conversation
 * it describes: the SPA can stream several sessions at once, and one
 * thread's cost must never appear above another thread's composer.
 *
 * Features:
 * - Compact tab-like design that sits on top of the chat input
 * - Dismissible with X button
 * - Accessible with proper ARIA attributes
 * - Light/dark mode support
 *
 * With `features.quotaRequests` on (quota-requests spec §5.1) the warning tab
 * gains a trailing *Request more* from the soft limit up, and a blocked user
 * gets a tab of its own with *Request more quota*. The funding notice — spend
 * now charged to a fund — is shown whatever the flag says: it describes an
 * extension that exists, and charging someone's fund silently is the one
 * thing it is there to prevent.
 */
@Component({
  selector: 'app-quota-warning-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, QuotaRequestActionComponent],
  providers: [
    provideIcons({ heroBanknotes, heroChatBubbleLeftRight, heroExclamationTriangle, heroNoSymbol, heroXMark }),
  ],
  template: `
    @if (quotaWarningService.hasVisibleFundingNotice()) {
      <div class="flex justify-center">
        <div
          class="inline-flex items-center gap-1.5 px-3 py-1 text-xs rounded-t-lg border border-b-0 animate-fade-in bg-white dark:bg-slate-800 border-gray-300 text-gray-700 dark:border-gray-600 dark:text-gray-300"
          role="status"
          [attr.aria-live]="'polite'"
          data-testid="quota-funding-notice"
        >
          <ng-icon name="heroBanknotes" class="size-3 shrink-0" />
          <span class="font-medium">{{ quotaWarningService.fundingNotice()?.message }}</span>
          <button
            type="button"
            (click)="dismissFundingNotice($event)"
            class="p-0.5 -mr-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
            aria-label="Dismiss funding notice"
          >
            <ng-icon name="heroXMark" class="size-3" />
          </button>
        </div>
      </div>
    }
    @if (showBlockedTab()) {
      <div class="flex justify-center">
        <div
          class="inline-flex flex-wrap items-center justify-center gap-x-1.5 px-3 py-1 text-xs rounded-t-lg border border-b-0 animate-fade-in bg-white dark:bg-slate-800 border-state-danger-400 text-state-danger-700 dark:border-state-danger-500 dark:text-state-danger-300"
          role="status"
          [attr.aria-live]="'polite'"
          data-testid="quota-blocked-tab"
        >
          <ng-icon name="heroNoSymbol" class="size-3 shrink-0" />
          <span class="font-medium">Quota reached ·</span>
          <app-quota-request-action label="Request more quota" />
        </div>
      </div>
    }
    @if (showSessionNotice()) {
      <div class="flex justify-center">
        <div
          class="inline-flex items-center gap-1.5 px-3 py-1 text-xs rounded-t-lg border border-b-0 animate-fade-in bg-white dark:bg-slate-800 border-gray-300 text-gray-700 dark:border-gray-600 dark:text-gray-300"
          role="status"
          [attr.aria-live]="'polite'"
        >
          <ng-icon name="heroChatBubbleLeftRight" class="size-3 shrink-0" />
          <span class="font-medium">{{ sessionNoticeText() }}</span>
          <button
            type="button"
            (click)="dismissSessionNotice($event)"
            class="p-0.5 -mr-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
            aria-label="Dismiss conversation cost notice"
          >
            <ng-icon name="heroXMark" class="size-3" />
          </button>
        </div>
      </div>
    }
    @if (quotaWarningService.hasVisibleWarning()) {
      <div class="flex justify-center">
        <div
          class="inline-flex items-center gap-1.5 px-3 py-1 text-xs rounded-t-lg border border-b-0 animate-fade-in bg-white dark:bg-slate-800"
          [class.border-state-warning-400]="quotaWarningService.severity() === 'warning'"
          [class.text-state-warning-700]="quotaWarningService.severity() === 'warning'"
          [class.dark:border-state-warning-500]="quotaWarningService.severity() === 'warning'"
          [class.dark:text-state-warning-300]="quotaWarningService.severity() === 'warning'"
          [class.border-state-danger-400]="quotaWarningService.severity() === 'critical'"
          [class.text-state-danger-700]="quotaWarningService.severity() === 'critical'"
          [class.dark:border-state-danger-500]="quotaWarningService.severity() === 'critical'"
          [class.dark:text-state-danger-300]="quotaWarningService.severity() === 'critical'"
          role="status"
          [attr.aria-live]="'polite'"
        >
          <ng-icon
            name="heroExclamationTriangle"
            class="size-3 shrink-0"
          />
          <span class="font-medium">{{ messageText() }}</span>
          @if (offerRequest()) {
            <span aria-hidden="true">·</span>
            <app-quota-request-action />
          }
          <button
            type="button"
            (click)="dismiss($event)"
            class="p-0.5 -mr-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
            aria-label="Dismiss warning"
          >
            <ng-icon name="heroXMark" class="size-3" />
          </button>
        </div>
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
  `]
})
export class QuotaWarningBannerComponent {
  protected quotaWarningService = inject(QuotaWarningService);
  private sessionService = inject(SessionService);
  private features = inject(FEATURES);

  /** Blocked, and this deployment lets users ask for more. */
  protected showBlockedTab = computed(
    () => this.features.quotaRequests && this.quotaWarningService.isQuotaExceeded(),
  );

  /** From the soft limit up, the warning tab offers a request. */
  protected offerRequest = computed(() => {
    const warning = this.quotaWarningService.activeWarning();
    return this.features.quotaRequests && !!warning && warning.percentageUsed >= REQUEST_PROMPT_PERCENT;
  });

  dismissFundingNotice(event: Event): void {
    event.stopPropagation();
    this.quotaWarningService.dismissFundingNotice();
  }

  /** Only show the notice on the conversation it describes. */
  protected showSessionNotice = computed(() => {
    if (!this.quotaWarningService.hasVisibleSessionNotice()) return false;

    const notice = this.quotaWarningService.sessionNotice();
    const viewedSessionId = this.sessionService.currentSession().sessionId;
    return !!notice && notice.sessionId === viewedSessionId;
  });

  /** Compact per-session notice text */
  protected sessionNoticeText = computed(() => {
    const notice = this.quotaWarningService.sessionNotice();
    if (!notice) return '';

    return `This conversation: ${this.quotaWarningService.formattedSessionUsage()} of your quota`;
  });

  dismissSessionNotice(event: Event): void {
    event.stopPropagation();
    this.quotaWarningService.dismissSessionNotice();
  }

  /** Compact message text */
  messageText = computed(() => {
    const warning = this.quotaWarningService.activeWarning();
    if (!warning) return '';

    const remaining = this.quotaWarningService.formattedRemaining();

    if (warning.percentageUsed >= 90) {
      return `${warning.warningLevel} usage - ${remaining} remaining`;
    }
    return `${warning.warningLevel} of quota used - ${remaining} remaining`;
  });

  dismiss(event: Event): void {
    event.stopPropagation();
    this.quotaWarningService.dismissWarning();
  }
}
