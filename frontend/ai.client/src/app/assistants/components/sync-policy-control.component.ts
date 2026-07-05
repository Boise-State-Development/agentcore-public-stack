import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroChevronDown } from '@ng-icons/heroicons/outline';

import { SyncInterval, SyncPolicy } from '../models/sync-policy.model';

/** The select's "no policy / turn sync off" sentinel. */
export type SyncIntervalSelection = SyncInterval | 'manual';

/**
 * Per-source "Keep in sync" control for the assistant knowledge editor.
 *
 * Presentational only: renders the interval select, the state-appropriate
 * action (Sync now / Pause / Resume / Reconnect) and a status line, and
 * emits the user's intent. The page owns the policy state and the HTTP
 * round-trips — on failure it simply doesn't update the `policy` input and
 * the control stays where it was (the select reverts its own DOM value
 * eagerly so a failed mutation never leaves a stale selection visible).
 *
 * `paused_reauth` deliberately has no Resume action — the backend rejects
 * that resume with 409 because only a fresh OAuth consent can fix it — so
 * the control renders the Reconnect affordance instead.
 */
@Component({
  selector: 'app-sync-policy-control',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArrowPath, heroChevronDown })],
  template: `
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
      <div class="relative inline-flex">
        <select
          [attr.aria-label]="'Auto-sync schedule for ' + (sourceName() || 'this source')"
          [disabled]="busy()"
          [value]="selectValue()"
          (change)="onSelectChange($event)"
          class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pl-2.5 pr-8 text-xs/5 text-gray-700 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
        >
          <option value="manual">Don't auto-sync</option>
          <option value="daily">Sync daily</option>
          <option value="weekly">Sync weekly</option>
          <option value="monthly">Sync monthly</option>
        </select>
        <ng-icon
          name="heroChevronDown"
          class="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500"
          aria-hidden="true"
        />
      </div>

      @if (policy(); as p) {
        @if (p.state === 'active') {
          <button
            type="button"
            (click)="runNow.emit()"
            [disabled]="busy()"
            [attr.aria-label]="'Sync ' + (sourceName() || 'this source') + ' now'"
            class="inline-flex items-center gap-1 rounded-2xl px-2 py-1 text-xs/5 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
          >
            <ng-icon name="heroArrowPath" class="size-3.5" aria-hidden="true" />
            Sync now
          </button>
          <button
            type="button"
            (click)="pause.emit()"
            [disabled]="busy()"
            [attr.aria-label]="'Pause sync for ' + (sourceName() || 'this source')"
            class="inline-flex items-center rounded-2xl px-2 py-1 text-xs/5 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
          >
            Pause
          </button>
        } @else if (p.state === 'paused_reauth') {
          <button
            type="button"
            (click)="reconnect.emit()"
            [disabled]="busy()"
            class="inline-flex items-center rounded-2xl px-2 py-1 text-xs/5 font-medium text-blue-600 hover:bg-blue-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-blue-400 dark:hover:bg-blue-900/20"
          >
            Reconnect {{ reconnectLabel() || 'source' }}
          </button>
        } @else {
          <button
            type="button"
            (click)="resume.emit()"
            [disabled]="busy()"
            [attr.aria-label]="'Resume sync for ' + (sourceName() || 'this source')"
            class="inline-flex items-center rounded-2xl px-2 py-1 text-xs/5 font-medium text-blue-600 hover:bg-blue-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-blue-400 dark:hover:bg-blue-900/20"
          >
            Resume
          </button>
        }
      }
    </div>
    @if (statusText(); as text) {
      <p
        class="mt-1 flex items-center gap-1.5 text-xs/5"
        [class.text-amber-600]="statusTone() === 'warn'"
        [class.dark:text-amber-400]="statusTone() === 'warn'"
        [class.text-gray-500]="statusTone() !== 'warn'"
        [class.dark:text-gray-400]="statusTone() !== 'warn'"
      >
        <span
          aria-hidden="true"
          class="size-1.5 shrink-0 rounded-full"
          [class.bg-green-500]="statusDot() === 'ok'"
          [class.bg-amber-500]="statusDot() === 'warn'"
          [class.bg-gray-400]="statusDot() === 'idle'"
        ></span>
        {{ text }}
      </p>
    }
  `,
})
export class SyncPolicyControlComponent {
  /** The covering policy, or null when the source is manual-only. */
  readonly policy = input<SyncPolicy | null>(null);
  /** Disables all controls while the page has a mutation in flight. */
  readonly busy = input(false);
  /** Source display name, used for the controls' accessible names. */
  readonly sourceName = input('');
  /** Provider display name for the paused_reauth affordance (e.g. "Google Drive"). */
  readonly reconnectLabel = input('');
  /**
   * The source's last successful sync, straight from the document record.
   * Used to keep a "Last synced …" line visible even when the source is
   * manual-only (no governing policy), where `policy` is null.
   */
  readonly lastSyncedAt = input<string | null>(null);

  readonly intervalSelected = output<SyncIntervalSelection>();
  readonly runNow = output<void>();
  readonly pause = output<void>();
  readonly resume = output<void>();
  readonly reconnect = output<void>();

  readonly selectValue = computed<SyncIntervalSelection>(
    () => this.policy()?.interval ?? 'manual',
  );

  readonly statusTone = computed<'muted' | 'warn'>(() => {
    const p = this.policy();
    if (!p) return 'muted';
    if (p.state === 'paused_error' || p.state === 'paused_reauth') return 'warn';
    if (p.state === 'active' && p.lastResult === 'failed') return 'warn';
    return 'muted';
  });

  /** Colour of the status-line dot: green healthy, amber attention, grey idle. */
  readonly statusDot = computed<'ok' | 'warn' | 'idle'>(() => {
    if (this.statusTone() === 'warn') return 'warn';
    return this.policy()?.state === 'active' ? 'ok' : 'idle';
  });

  readonly statusText = computed<string>(() => {
    const p = this.policy();
    // Manual-only (no governing policy): still surface when the file last
    // refreshed, using the document's own timestamp.
    if (!p) {
      const ts = this.lastSyncedAt();
      return ts ? this.syncedPhrase('Last synced', ts) : '';
    }
    switch (p.state) {
      case 'active': {
        const parts: string[] = [];
        if (p.lastResult === 'failed') {
          parts.push(
            p.lastSyncAt
              ? this.syncedPhrase('Last sync failed', p.lastSyncAt)
              : 'Last sync failed',
          );
        } else if (p.lastSyncAt) {
          parts.push(this.syncedPhrase('Synced', p.lastSyncAt));
        } else {
          parts.push('Not synced yet');
        }
        if (p.nextSyncAt) {
          parts.push(`next sync ${this.formatUntil(p.nextSyncAt)}`);
        }
        return parts.join(' · ');
      }
      case 'paused_user': {
        const ts = p.lastSyncAt ?? this.lastSyncedAt();
        return ts ? `Paused · last synced ${this.formatAgo(ts)}` : 'Paused';
      }
      case 'paused_error':
        return this.pausedText(p.stateReason, 'Paused after repeated failures');
      case 'paused_inactive':
        return this.pausedText(
          p.stateReason,
          'Paused while the assistant is inactive — resumes on next use',
        );
      case 'paused_reauth':
        return this.pausedText(p.stateReason, 'Reconnect the content source to resume syncing');
    }
  });

  onSelectChange(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const chosen = select.value as SyncIntervalSelection;
    const current = this.selectValue();
    // Revert the DOM eagerly: the select only moves for real once the page
    // confirms the mutation and the `policy` input re-renders it. A failed
    // request therefore never strands the select on a value that isn't true.
    select.value = current;
    if (chosen === current) {
      return;
    }
    this.intervalSelected.emit(chosen);
  }

  private pausedText(reason: string | null | undefined, fallback: string): string {
    return reason ? `Paused — ${reason}` : `Paused — ${fallback}`;
  }

  /**
   * "{verb} {relative} · {absolute}" — pairs a scannable relative age with an
   * exact date/time so the reader gets both "how long ago" and "exactly when".
   */
  private syncedPhrase(verb: string, iso: string): string {
    const ago = this.formatAgo(iso);
    const abs = this.formatAbsolute(iso);
    if (!ago && !abs) return verb;
    if (!abs) return `${verb} ${ago}`;
    if (!ago) return `${verb} ${abs}`;
    return `${verb} ${ago} · ${abs}`;
  }

  /**
   * Parse a backend ISO timestamp into a Date, tolerating a historical bug
   * where timestamps were emitted with BOTH an offset and a "Z"
   * (e.g. "2026-07-05T16:00:00+00:00Z"). That string is invalid ISO 8601 and
   * unparseable by strict engines (Safari) — strip the redundant trailing Z so
   * already-persisted policies still render while the backend is corrected.
   */
  private parseIso(iso: string): Date {
    return new Date(iso.replace(/([+-]\d{2}:\d{2})Z$/, '$1'));
  }

  private formatAbsolute(iso: string): string {
    const then = this.parseIso(iso);
    if (Number.isNaN(then.getTime())) return '';
    return then.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  }

  private formatAgo(iso: string): string {
    const parsed = this.parseIso(iso);
    const then = parsed.getTime();
    if (Number.isNaN(then)) return '';
    const diffMins = Math.floor((Date.now() - then) / 60_000);
    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 30) return `${diffDays}d ago`;
    return parsed.toLocaleDateString();
  }

  private formatUntil(iso: string): string {
    const then = this.parseIso(iso).getTime();
    if (Number.isNaN(then)) return '';
    const diffMins = Math.ceil((then - Date.now()) / 60_000);
    // "due now" covers the run-now window: the dispatcher sweeps every
    // 15 minutes, so a due policy runs within the next tick.
    if (diffMins <= 15) return 'due now';
    if (diffMins < 60) return `in ${diffMins}m`;
    const diffHours = Math.round(diffMins / 60);
    if (diffHours < 24) return `in ${diffHours}h`;
    return `in ${Math.round(diffHours / 24)}d`;
  }
}
