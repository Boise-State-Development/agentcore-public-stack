import { Injectable, computed, inject, signal } from '@angular/core';
import { SessionService } from '../session/session.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import type { SteeringAppliedEvent } from '../../../shared/utils/stream-parser';

/** One queued follow-up, as the composer holds it and the resume path sends it. */
export interface CarriedSteerEntry {
  id: string;
  text: string;
}

/**
 * Mid-turn steering state, shared between the composer (which owns the queue)
 * and the stream parser (which sees the acks). See
 * docs/specs/mid-turn-steering.md.
 *
 * Written by the parser, read by the composer — one direction only, so neither
 * has to know about the other and there is no DI cycle. Same shape as
 * `CompactionSummaryService`.
 */
@Injectable({ providedIn: 'root' })
export class SteeringService {
  private readonly sessionService = inject(SessionService);
  private readonly consentService = inject(OAuthConsentService);
  private readonly toolApprovalService = inject(ToolApprovalService);

  /**
   * Entry ids the backend has confirmed are in conversation history. The
   * composer drops the matching queued entry on seeing one here.
   *
   * Ids, not entries: the composer already holds the text, and an id-only
   * channel means a duplicate ack (a reconnect, a replayed frame) is a no-op
   * rather than a second bubble.
   */
  private readonly appliedIds = signal<string[]>([]);
  readonly applied = this.appliedIds.asReadonly();

  /**
   * Sessions whose current turn has called at least one tool.
   *
   * The composer's placeholder promises different things depending on whether
   * a follow-up can land mid-turn, and only a turn with tool boundaries has
   * anywhere to put one. This is deliberately **sticky for the turn** rather
   * than "a tool is running right this instant": agent loops that call one
   * tool almost always call another, and a per-tool flag would flip the
   * placeholder's wording on and off several times while the user is typing
   * into it.
   */
  private readonly turnUsedTools = signal<Record<string, boolean>>({});

  /**
   * Set once the environment answers 404 to a steer — the feature is off
   * here. Remembered for the tab session so we stop asking; every later
   * follow-up takes the end-of-turn path without a round trip.
   */
  private readonly unavailable = signal(false);
  readonly isUnavailable = this.unavailable.asReadonly();

  /** Whether a follow-up queued right now could plausibly land mid-turn. */
  canSteer(sessionId: string | null): boolean {
    if (!sessionId || this.unavailable()) return false;
    return this.turnUsedTools()[sessionId] === true;
  }

  /** A tool ran on this session's current turn. */
  markToolUsed(sessionId: string): void {
    if (this.turnUsedTools()[sessionId]) return;
    this.turnUsedTools.update((map) => ({ ...map, [sessionId]: true }));
  }

  /** A new turn started — nothing has been called on it yet. */
  startTurn(sessionId: string): void {
    if (!this.turnUsedTools()[sessionId]) return;
    this.turnUsedTools.update((map) => {
      const { [sessionId]: _dropped, ...rest } = map;
      return rest;
    });
  }

  /**
   * Arm a follow-up against the turn currently streaming for this session.
   *
   * Resolves `true` only when the backend confirms it landed on a live turn.
   * Every other outcome — no live turn, the feature off, a network failure —
   * resolves `false`, because they all mean the same thing to the caller: keep
   * the entry queued and let the end-of-turn flush send it. Failing soft here
   * is the whole posture; the user's text is never at risk, only its timing.
   */
  async arm(sessionId: string, entryId: string, text: string): Promise<boolean> {
    if (this.unavailable()) return false;
    try {
      return await this.sessionService.steerRunningTurn(sessionId, entryId, text);
    } catch (error) {
      if (this.isFeatureOff(error)) {
        this.unavailable.set(true);
      }
      return false;
    }
  }

  /** Withdraw an armed entry the user removed from the composer. */
  async withdraw(sessionId: string, entryId: string): Promise<void> {
    try {
      await this.sessionService.withdrawSteer(sessionId, entryId);
    } catch {
      // The entry is either already consumed (and will render as a message)
      // or unreachable. Neither is worth surfacing on an action the user has
      // already seen take effect in the composer.
    }
  }

  /** Record a `steering_applied` SSE event. Called by the stream parser. */
  recordApplied(event: SteeringAppliedEvent): void {
    this.appliedIds.update((ids) =>
      ids.includes(event.entryId) ? ids : [...ids, event.entryId],
    );
  }

  /** Drop an ack the composer has acted on, so the list cannot grow unbounded. */
  consumeApplied(entryId: string): void {
    this.appliedIds.update((ids) => ids.filter((id) => id !== entryId));
  }

  // -- paused turns -------------------------------------------------------
  //
  // A turn paused for OAuth consent or tool approval has no running loop to
  // steer, and the pause released its lease — inbox and all — when the stream
  // closed. See docs/specs/mid-turn-steering.md ("Paused turns").

  /** Sessions with a prompt on screen waiting for the user to answer it. */
  private readonly awaitingAnswer = computed<Set<string>>(() => {
    const sessions = new Set<string>();
    for (const request of this.consentService.pending()) {
      if (request.sessionId && request.interruptId) sessions.add(request.sessionId);
    }
    for (const request of this.toolApprovalService.pending()) {
      if (request.sessionId) sessions.add(request.sessionId);
    }
    return sessions;
  });

  /**
   * Whether the composer should hold its queue rather than flushing it.
   *
   * True only while this session has a *resumable* prompt awaiting an answer.
   * Flushing then would start a brand-new turn, which abandons the paused one
   * the user is in the middle of answering — and, worse, can race the resume
   * that follows into the single-flight guard. The follow-up waits with the
   * prompt and rides the resumed turn instead.
   *
   * The hold is bounded by an action the user already has: answering the
   * prompt resumes and carries the queue, and dismissing it clears the pending
   * request, which drops this to false and lets the ordinary flush run. A
   * pre-flight consent (no `interruptId`, nothing paused) never holds, because
   * there is no turn to resume.
   */
  shouldHoldQueue(sessionId: string | null): boolean {
    return !!sessionId && this.awaitingAnswer().has(sessionId);
  }

  /**
   * The composer's live queue, mirrored per session so the resume path can
   * carry it without reaching into a component.
   *
   * A mirror, not a second source of truth: the composer owns the queue and
   * republishes on every change, and nothing here ever writes back.
   */
  private readonly heldQueues = signal<Record<string, CarriedSteerEntry[]>>({});

  publishQueue(sessionId: string | null, entries: CarriedSteerEntry[]): void {
    if (!sessionId) return;
    this.heldQueues.update((map) => ({ ...map, [sessionId]: entries }));
  }

  /** Entries a resume request should carry into the turn it restarts. */
  carriedFor(sessionId: string): CarriedSteerEntry[] {
    return this.heldQueues()[sessionId] ?? [];
  }

  /** Clear all state — called on session change. */
  reset(): void {
    this.appliedIds.set([]);
    this.turnUsedTools.set({});
    this.heldQueues.set({});
  }

  private isFeatureOff(error: unknown): boolean {
    return (
      typeof error === 'object' &&
      error !== null &&
      (error as { status?: number }).status === 404
    );
  }
}
