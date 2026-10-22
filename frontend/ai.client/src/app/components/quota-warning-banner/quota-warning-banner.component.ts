import { Component, ChangeDetectionStrategy, inject, computed } from '@angular/core';
import { QuotaWarningService } from '../../services/quota/quota-warning.service';
import { SessionService } from '../../session/services/session/session.service';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroChatBubbleLeftRight,
  heroExclamationTriangle,
  heroXMark,
} from '@ng-icons/heroicons/outline';

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
 */
@Component({
  selector: 'app-quota-warning-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroChatBubbleLeftRight, heroExclamationTriangle, heroXMark }),
  ],
  template: `
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
