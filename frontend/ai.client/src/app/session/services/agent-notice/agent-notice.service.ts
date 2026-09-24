import { Injectable, signal } from '@angular/core';
import { AgentNoticeEvent } from '../../../shared/utils/stream-parser';

/**
 * The `agent_notice` a project's agent sent for each session's live turn:
 * "this task runs without X, which you can't use" (shared-projects §9.6).
 *
 * Kept per session because several conversations can stream at once, and one
 * task's notice must never appear above another's composer. Nothing is
 * persisted — the backend re-derives the notice every turn — so a reload drops
 * it. A dismissed notice stays dismissed for that session while the next turns
 * keep saying the same thing; a different message shows again.
 */
@Injectable({ providedIn: 'root' })
export class AgentNoticeService {
  private readonly notices = signal<ReadonlyMap<string, AgentNoticeEvent>>(new Map());
  /** sessionId → the message the user dismissed there. */
  private readonly dismissed = new Map<string, string>();

  /** The notice to show for a session, or null. Signal-backed. */
  noticeFor(sessionId: string | null | undefined): AgentNoticeEvent | null {
    if (!sessionId) return null;
    return this.notices().get(sessionId) ?? null;
  }

  set(sessionId: string, notice: AgentNoticeEvent): void {
    if (this.dismissed.get(sessionId) === notice.message) return;
    this.dismissed.delete(sessionId);
    this.notices.update(map => new Map(map).set(sessionId, notice));
  }

  dismiss(sessionId: string): void {
    const notice = this.notices().get(sessionId);
    if (!notice) return;
    this.dismissed.set(sessionId, notice.message);
    this.notices.update(map => {
      const next = new Map(map);
      next.delete(sessionId);
      return next;
    });
  }
}
