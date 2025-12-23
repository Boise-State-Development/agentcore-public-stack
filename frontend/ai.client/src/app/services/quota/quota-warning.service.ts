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

  /** Timestamp when the warning was received */
  private warningTimestampSignal = signal<Date | null>(null);

  /** Whether the user has dismissed the current warning */
  private isDismissedSignal = signal<boolean>(false);

  // =========================================================================
  // Public Readonly Signals
  // =========================================================================

  /** The active quota warning */
  readonly activeWarning = this.activeWarningSignal.asReadonly();

  /** The quota exceeded state */
  readonly quotaExceeded = this.quotaExceededSignal.asReadonly();

  /** Whether there's a visible warning to show */
  readonly hasVisibleWarning = computed(() => {
    return this.activeWarningSignal() !== null && !this.isDismissedSignal();
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
   * Clear all quota state (warnings and exceeded)
   */
  clearAll(): void {
    this.clearWarning();
    this.clearQuotaExceeded();
  }

  /**
   * Reset dismissed state to show warning again on next occurrence
   */
  resetDismissed(): void {
    this.isDismissedSignal.set(false);
  }
}
