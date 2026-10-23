import { Injectable, computed, inject, signal } from '@angular/core';
import { UserSettingsService } from '../../../services/user-settings.service';

/**
 * Incognito chats (#1738): a conversation that reads the user's long-term
 * memory but never adds to it.
 *
 * The choice is made in the new-chat composer before the first message and is
 * fixed after that. The backend stores it on the session row when the first
 * turn (or voice connection) creates it, and ignores the request's value on
 * every later turn, so this service only carries the choice to that first
 * request and remembers it for the sidenav until the row's preferences arrive.
 */
@Injectable({ providedIn: 'root' })
export class IncognitoChatService {
  private readonly userSettings = inject(UserSettingsService);

  /** The new-chat composer's toggle. Every new chat starts with it off. */
  private readonly _pending = signal(false);
  readonly pending = this._pending.asReadonly();

  /** Conversations started incognito in this tab. */
  private readonly sessionIds = signal<ReadonlySet<string>>(new Set());

  /**
   * Whether the toggle is offered at all: only when the account's memory mode
   * is "on". In "read only" and "off" nothing is learned anyway.
   */
  readonly available = computed(() => {
    const resource = this.userSettings.settingsResource;
    if (!resource.hasValue()) return false;
    return (resource.value()?.memoryMode ?? 'on') === 'on';
  });

  toggle(): void {
    this._pending.update((on) => !on);
  }

  reset(): void {
    this._pending.set(false);
  }

  /**
   * Hand the composer's choice to the conversation it is starting, and reset
   * the toggle. Returns whether that conversation is incognito.
   */
  claim(sessionId: string): boolean {
    if (this._pending() && this.available()) {
      this.sessionIds.update((ids) => new Set(ids).add(sessionId));
    }
    this._pending.set(false);
    return this.isIncognito(sessionId);
  }

  /** Whether this tab started the conversation incognito. */
  isIncognito(sessionId: string): boolean {
    return this.sessionIds().has(sessionId);
  }
}
