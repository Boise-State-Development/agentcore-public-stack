import { Injectable } from '@angular/core';

/**
 * Providers whose *pre-flight* consent prompt the signed-in user dismissed,
 * remembered across conversations and reloads.
 *
 * A pre-flight `oauth_required` event (no `interruptId`) is re-emitted on
 * every turn until consent lands, in every conversation, because the tool
 * keeps failing its `tools/list` pre-flight. "Not now" is therefore a
 * decision about the provider, not about one conversation: held in memory and
 * cleared on conversation change, it came back on the next turn of the next
 * chat. Interrupt-driven prompts never consult this — a paused turn must
 * always be answerable.
 *
 * The entry names the account it belongs to and is only honoured for that
 * account ({@link adopt}), so someone else signing in on this browser starts
 * from scratch even if the previous person closed the tab instead of signing
 * out. Sign-out ({@link clear}) and a completed consent ({@link forget}) drop
 * it.
 *
 * Storage is best-effort: a blocked or full `localStorage` (private window,
 * storage-disabled browser) degrades to a dismissal that lasts until reload.
 * No dependencies, so `SessionService` (auth) can inject it without a cycle.
 */

/** `localStorage` key for {@link StoredPreflightDismissals}. */
export const PREFLIGHT_DISMISSALS_KEY = 'oauth-preflight-dismissed';

interface StoredPreflightDismissals {
  owner: string;
  providers: string[];
}

@Injectable({ providedIn: 'root' })
export class PreflightDismissalStorageService {
  /** The signed-in account; null until {@link adopt} runs. */
  private owner: string | null = null;

  /** Last known set: the whole truth when storage is blocked or unowned. */
  private memory = new Set<string>();

  /**
   * Claim the stored dismissals for `userId`, dropping them if they belong to
   * someone else. Called on every successful session bootstrap.
   */
  adopt(userId: string): void {
    if (this.owner !== userId) {
      this.memory = new Set();
    }
    this.owner = userId;
    const stored = this.read();
    if (stored && stored.owner !== userId) {
      this.remove();
    }
    this.memory = this.current();
  }

  has(providerId: string): boolean {
    return this.current().has(providerId);
  }

  add(providerId: string): void {
    const next = this.current();
    next.add(providerId);
    this.persist(next);
  }

  /** Consent landed for `providerId`, so a later disconnect may prompt again. */
  forget(providerId: string): void {
    const next = this.current();
    if (next.delete(providerId)) {
      this.persist(next);
    }
  }

  /** Sign-out: the next person on this browser is prompted from scratch. */
  clear(): void {
    this.owner = null;
    this.memory = new Set();
    this.remove();
  }

  /**
   * The owner's stored set, re-read each time so a dismissal (or consent) in
   * another tab takes effect here too; the in-memory set when there is none.
   */
  private current(): Set<string> {
    const stored = this.read();
    if (this.owner && stored?.owner === this.owner) {
      return new Set(stored.providers);
    }
    return new Set(this.memory);
  }

  private persist(next: Set<string>): void {
    this.memory = next;
    // Without an owner the entry could not be attributed on the next load;
    // the in-memory set still covers this page.
    if (!this.owner) return;
    if (next.size === 0) {
      this.remove();
      return;
    }
    const value: StoredPreflightDismissals = {
      owner: this.owner,
      providers: [...next].sort(),
    };
    try {
      localStorage.setItem(PREFLIGHT_DISMISSALS_KEY, JSON.stringify(value));
    } catch {
      // Best-effort; see above.
    }
  }

  private read(): StoredPreflightDismissals | null {
    try {
      const raw = localStorage.getItem(PREFLIGHT_DISMISSALS_KEY);
      if (!raw) return null;
      const parsed: unknown = JSON.parse(raw);
      if (
        parsed && typeof parsed === 'object' &&
        typeof (parsed as StoredPreflightDismissals).owner === 'string' &&
        Array.isArray((parsed as StoredPreflightDismissals).providers)
      ) {
        const { owner, providers } = parsed as StoredPreflightDismissals;
        return { owner, providers: providers.filter((p): p is string => typeof p === 'string') };
      }
    } catch {
      // Unreadable storage or a corrupt entry: treat as nothing dismissed.
    }
    return null;
  }

  private remove(): void {
    try {
      localStorage.removeItem(PREFLIGHT_DISMISSALS_KEY);
    } catch {
      // Best-effort; see above.
    }
  }
}
