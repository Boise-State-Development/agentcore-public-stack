import { SessionMetadata } from '../models/session-metadata.model';

/**
 * The cost/context aggregates a session row carries, as persisted by the
 * backend's `_bump_session_aggregates`. They seed the composer's cost badge
 * when a conversation opens; live turns then add to them over SSE.
 */
export interface PersistedAggregates {
  sessionId: string;
  totalCost?: number;
  lastContextTokens?: number;
  contextWindow?: number;
}

/** Project a session row onto its aggregates, or `null` for no session. */
export function persistedAggregatesOf(
  session: SessionMetadata | null | undefined,
): PersistedAggregates | null {
  if (!session?.sessionId) return null;
  return {
    sessionId: session.sessionId,
    totalCost: session.totalCost,
    lastContextTokens: session.lastContextTokens,
    contextWindow: session.contextWindow,
  };
}

/**
 * Equality for a `computed` over {@link persistedAggregatesOf}, so the badge
 * is re-seeded only when the session or one of its aggregates changes.
 *
 * `currentSession` is rewritten for reasons that have nothing to do with
 * cost (a first turn's preference read-back, title sync, a model switch's
 * preference mirror, an unread toggle), and each rewrite still carries the
 * aggregates as they were when the row was loaded. Seeding on every rewrite
 * reset a live turn's cost and context to that stale row: on a new
 * conversation, $0.00 and "Context window not measured yet".
 */
export function samePersistedAggregates(
  a: PersistedAggregates | null,
  b: PersistedAggregates | null,
): boolean {
  return (
    a?.sessionId === b?.sessionId &&
    a?.totalCost === b?.totalCost &&
    a?.lastContextTokens === b?.lastContextTokens &&
    a?.contextWindow === b?.contextWindow
  );
}
