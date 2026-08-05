import { Injectable, signal, computed } from '@angular/core';

/**
 * Interface representing a quota warning from the SSE stream
 */
export interface QuotaWarning {
  type: 'quota_warning';
  warningLevel: string;  // e.g., "80%", "90%"
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  remaining: number;
  message: string;
}

/**
 * Interface representing a per-session quota notice from the SSE stream.
 *
 * Answers a different question than QuotaWarning: not "how much of your
 * month is gone" but "how much of your month has THIS conversation spent".
 * A single long thread can be most of a monthly budget while the per-user
 * percentage still looks unremarkable — the failure mode that spent a
 * faculty user's quota in five days.
 */
export interface QuotaSessionNotice {
  type: 'quota_session_notice';
  sessionId: string;
  sessionCost: number;
  quotaLimit: number;
  sessionPercentageOfLimit: number;
  thresholdPercentage: number;
  message: string;
}

/**
 * Interface representing a quota exceeded event from the SSE stream.
 * This is sent when the user has exceeded their usage limit and the
 * response is streamed as an assistant message for better UX.
 */
export interface QuotaExceeded {
  type: 'quota_exceeded';
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  periodType: string;  // 'monthly' or 'daily'
  tierName?: string;
  resetInfo: string;
  message: string;
}

/**
 * Service for managing quota warning and quota exceeded state
 *
 * Handles quota warnings and quota exceeded events received from the SSE stream
 * and exposes reactive signals for UI components to display appropriate feedback.
 */
@Injectable({
  providedIn: 'root'
})
export class QuotaWarningService {
  /** The current active quota warning, null if no warning */
  private activeWarningSignal = signal<QuotaWarning | null>(null);

  /** The current quota exceeded state, null if not exceeded */
  private quotaExceededSignal = signal<QuotaExceeded | null>(null);

  /** The current per-session notice, null if this conversation is not heavy */
  private sessionNoticeSignal = signal<QuotaSessionNotice | null>(null);

  /** Timestamp when the warning was received */
  private warningTimestampSignal = signal<Date | null>(null);

  /** Whether the user has dismissed the current warning */
  private isDismissedSignal = signal<boolean>(false);

  /** Whether the user has dismissed the current session notice */
  private isSessionNoticeDismissedSignal = signal<boolean>(false);

  // =========================================================================
  // Public Readonly Signals
  // =========================================================================

  /** The active quota warning */
  readonly activeWarning = this.activeWarningSignal.asReadonly();

  /** The quota exceeded state */
  readonly quotaExceeded = this.quotaExceededSignal.asReadonly();

  /** The active per-session notice */
  readonly sessionNotice = this.sessionNoticeSignal.asReadonly();

  /** Whether there's a visible warning to show */
  readonly hasVisibleWarning = computed(() => {
    return this.activeWarningSignal() !== null && !this.isDismissedSignal();
  });

  /** Whether there's a visible per-session notice to show.
   *  Suppressed once the quota is actually exceeded — at that point the
   *  exceeded state is the message that matters. */
  readonly hasVisibleSessionNotice = computed(() => {
    return (
      this.sessionNoticeSignal() !== null &&
      !this.isSessionNoticeDismissedSignal() &&
      this.quotaExceededSignal() === null
    );
  });

  /** Formatted session cost against the quota (e.g. "$7.58 of $30.00") */
  readonly formattedSessionUsage = computed(() => {
    const notice = this.sessionNoticeSignal();
    if (!notice) return '';

    return `$${notice.sessionCost.toFixed(2)} of $${notice.quotaLimit.toFixed(2)}`;
  });

  /** Whether quota has been exceeded (for UI to show special styling) */
  readonly isQuotaExceeded = computed(() => {
    return this.quotaExceededSignal() !== null;
  });

  /** Warning severity level for styling */
  readonly severity = computed<'warning' | 'critical' | 'exceeded' | null>(() => {
    // Quota exceeded takes precedence
    if (this.quotaExceededSignal()) return 'exceeded';

    const warning = this.activeWarningSignal();
    if (!warning) return null;

    // 90% or higher is critical, otherwise warning
    return warning.percentageUsed >= 90 ? 'critical' : 'warning';
  });

  /** Formatted usage display (e.g., "$8.00 / $10.00") */
  readonly formattedUsage = computed(() => {
    // Check quota exceeded first
    const exceeded = this.quotaExceededSignal();
    if (exceeded) {
      const current = exceeded.currentUsage.toFixed(2);
      const limit = exceeded.quotaLimit.toFixed(2);
      return `$${current} / $${limit}`;
    }

    const warning = this.activeWarningSignal();
    if (!warning) return '';

    const current = warning.currentUsage.toFixed(2);
    const limit = warning.quotaLimit.toFixed(2);
    return `$${current} / $${limit}`;
  });

  /** Formatted remaining amount */
  readonly formattedRemaining = computed(() => {
    const warning = this.activeWarningSignal();
    if (!warning) return '';

    return `$${warning.remaining.toFixed(2)}`;
  });

  /** Reset info for quota exceeded */
  readonly resetInfo = computed(() => {
    const exceeded = this.quotaExceededSignal();
    return exceeded?.resetInfo ?? '';
  });

  // =========================================================================
  // Public Methods
  // =========================================================================

  /**
   * Set a new quota warning from the SSE stream
   *
   * @param warning - The quota warning event data
   */
  setWarning(warning: QuotaWarning): void {
    // Only update if this is a new/different warning
    const current = this.activeWarningSignal();
    if (current?.warningLevel !== warning.warningLevel ||
        current?.currentUsage !== warning.currentUsage) {
      this.activeWarningSignal.set(warning);
      this.warningTimestampSignal.set(new Date());
      this.isDismissedSignal.set(false);
    }
  }

  /**
   * Dismiss the current warning
   * The warning will reappear on the next request if still over threshold
   */
  dismissWarning(): void {
    this.isDismissedSignal.set(true);
  }

  /**
   * Clear all warning state (e.g., on logout)
   */
  clearWarning(): void {
    this.activeWarningSignal.set(null);
    this.warningTimestampSignal.set(null);
    this.isDismissedSignal.set(false);
  }

  /**
   * Set the per-session notice from the SSE stream.
   *
   * The backend re-emits this every turn while the conversation is over the
   * share, so a dismissal only sticks until the number moves — the same
   * contract as the per-user warning. Switching conversations clears it
   * (see clearSessionNotice) rather than showing one thread's cost above
   * another thread's composer.
   */
  setSessionNotice(notice: QuotaSessionNotice): void {
    const current = this.sessionNoticeSignal();
    if (
      current?.sessionId !== notice.sessionId ||
      current?.sessionCost !== notice.sessionCost
    ) {
      this.sessionNoticeSignal.set(notice);
      this.isSessionNoticeDismissedSignal.set(false);
    }
  }

  /** Dismiss the current session notice */
  dismissSessionNotice(): void {
    this.isSessionNoticeDismissedSignal.set(true);
  }

  /** Clear session-notice state (e.g. when switching conversations) */
  clearSessionNotice(): void {
    this.sessionNoticeSignal.set(null);
    this.isSessionNoticeDismissedSignal.set(false);
  }

  /**
   * Set quota exceeded state from the SSE stream
   *
   * @param exceeded - The quota exceeded event data
   */
  setQuotaExceeded(exceeded: QuotaExceeded): void {
    this.quotaExceededSignal.set(exceeded);
    // Also clear any active warning since we're now at exceeded state
    this.activeWarningSignal.set(null);
  }

  /**
   * Clear quota exceeded state (e.g., after quota resets or on new session)
   */
  clearQuotaExceeded(): void {
    this.quotaExceededSignal.set(null);
  }

  /**
   * Clear all quota state (warnings, session notice, and exceeded)
   */
  clearAll(): void {
    this.clearWarning();
    this.clearSessionNotice();
    this.clearQuotaExceeded();
  }

  /**
   * Reset dismissed state to show warning again on next occurrence
   */
  resetDismissed(): void {
    this.isDismissedSignal.set(false);
  }
}
