import { Injectable } from '@angular/core';

/**
 * Which conversation a fork's turns pin their Runtime microVM to
 * (docs/specs/conversation-rewind-and-fork.md §7).
 *
 * A fork's first turn starts from its parent's live history, which exists
 * only in the microVM the parent ran in, and that history is what the prompt
 * cache holds. So every turn of a fork carries `affinity_session_id`: its root
 * conversation's id, which app-api pins the turn to instead of the fork's own.
 * Routing only: the backend checks the fork relation on its own session row
 * before it uses anything it finds there, and the pin is per user, so a wrong
 * value can only land a turn in another of the user's own microVMs.
 *
 * Filled from the fork response and from session metadata (`rootSessionId`),
 * so a reload re-learns it before the next send. A session it does not know
 * simply pins to itself, as every session did before forks.
 */
@Injectable({ providedIn: 'root' })
export class SessionAffinityService {
  private readonly roots = new Map<string, string>();

  record(sessionId: string, rootSessionId: string | null | undefined): void {
    if (rootSessionId && rootSessionId !== sessionId) {
      this.roots.set(sessionId, rootSessionId);
    }
  }

  /** The id to pin `sessionId`'s turns to, or null to pin to itself. */
  affinityFor(sessionId: string | null | undefined): string | null {
    return sessionId ? (this.roots.get(sessionId) ?? null) : null;
  }
}
