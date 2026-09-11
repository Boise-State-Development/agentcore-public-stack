import { Injectable, signal } from '@angular/core';
import type {
  AgentStatusEvent,
  ToolGroupSummaryEvent,
} from '../../../shared/utils/stream-parser';

/** What we know about one tool invocation beyond its raw input/result. */
export interface ToolInsight {
  /** Event-loop-measured execution time, from the `tool_end` status. */
  durationMs?: number;
  /**
   * Model-generated one-line summary of the BATCH this call belonged to.
   * Stored per call rather than per batch so the rail — which groups by
   * tool-use id, not by batch — can find it from any member of the group.
   */
  summary?: string;
  /** The batch this call was summarized with; dedupes a re-delivered event. */
  batchId?: string;
}

/**
 * Per-conversation registry of what the agent did with each tool call:
 * execution durations and model-generated batch summaries.
 *
 * Retention matches `McpAppStateService` rather than the viewed-session-scoped
 * services, and for the same reason. Both facts arrive on inline events that
 * never re-stream (`agent_status`, `tool_group_summary`), and the only
 * server-side replay is the `toolSummaries` sidecar on `GET /messages` — a
 * request `loadMessagesForSession` deliberately skips when the conversation's
 * messages are already in memory. A registry that reset on navigation would
 * have no way back: leaving a conversation and returning would silently
 * downgrade every rail from the model's prose to the deterministic formatter,
 * with no way to recover short of a hard refresh.
 *
 * Reads are scoped to a conversation so a `toolUseId` can only resolve inside
 * the conversation that produced it. Writes carry their own session id: the
 * streaming session is not necessarily the viewed one, and a background
 * conversation's insights should be there when the user navigates back.
 *
 * Durations are deliberately NOT persisted server-side. They are cheap to
 * re-derive as a nicety and worthless as a historical record — a reloaded
 * conversation shows summaries without timings, which reads fine, where a
 * duration invented on the client would be a number the user could not trust.
 */
@Injectable({ providedIn: 'root' })
export class ToolInsightService {
  private readonly bySession = signal<
    ReadonlyMap<string, ReadonlyMap<string, ToolInsight>>
  >(new Map());

  /**
   * The live status of the streaming turn, per conversation.
   *
   * Unlike the insights above this is genuinely ephemeral — it describes what
   * is happening *now* — so it is cleared at the end of every turn rather than
   * retained. A stale "Using list_courses" on a finished conversation would be
   * a lie, not a downgrade.
   */
  private readonly statusBySession = signal<
    ReadonlyMap<string, AgentStatusEvent>
  >(new Map());

  // -- reads ---------------------------------------------------------------

  /** Everything known about one tool call in one conversation. */
  get(sessionId: string, toolUseId: string): ToolInsight | undefined {
    return this.bySession().get(sessionId)?.get(toolUseId);
  }

  /**
   * The batch summary covering `toolUseIds`, if any member carries one.
   *
   * The rail asks with the whole group because grouping is a client-side
   * decision (consecutive tool calls across messages within a turn) that need
   * not line up with the backend's batches. Returning the first match is
   * correct for the common case — one batch per group — and for the rarer
   * merged case it gives the earliest batch's line, which is the one that
   * describes where the group started.
   */
  summaryFor(sessionId: string, toolUseIds: readonly string[]): string | undefined {
    const forSession = this.bySession().get(sessionId);
    if (!forSession) return undefined;
    for (const id of toolUseIds) {
      const summary = forSession.get(id)?.summary;
      if (summary) return summary;
    }
    return undefined;
  }

  /** The live status of a conversation's streaming turn, or undefined. */
  status(sessionId: string): AgentStatusEvent | undefined {
    return this.statusBySession().get(sessionId);
  }

  // -- writes --------------------------------------------------------------

  /**
   * Apply one `agent_status` transition.
   *
   * `tool_end` is the only phase that carries a durable fact (the duration),
   * so it writes through to the insight registry as well as updating the live
   * status. The rest are live-only.
   */
  recordStatus(sessionId: string, event: AgentStatusEvent): void {
    this.statusBySession.update(map => new Map(map).set(sessionId, event));

    if (
      event.phase === 'tool_end' &&
      event.toolUseId &&
      typeof event.durationMs === 'number'
    ) {
      this.mergeInsight(sessionId, event.toolUseId, {
        durationMs: event.durationMs,
      });
    }
  }

  /**
   * Clear the live status for a conversation.
   *
   * Called on the turn's falling edge (`done`, an error, an abort). Durations
   * and summaries already written are untouched — only the "right now" claim
   * goes away.
   */
  clearStatus(sessionId: string): void {
    this.statusBySession.update(map => {
      if (!map.has(sessionId)) return map;
      const next = new Map(map);
      next.delete(sessionId);
      return next;
    });
  }

  /**
   * Record a model-generated batch summary against every call in the batch.
   *
   * Last write wins per call: a re-delivered event for the same batch is
   * idempotent, and the (theoretical) case of two batches claiming one call
   * resolves to the most recent, which is the more informed one.
   */
  recordSummary(sessionId: string, event: ToolGroupSummaryEvent): void {
    const summary = event.summary?.trim();
    if (!summary || !event.toolUseIds?.length) return;

    this.bySession.update(map => {
      const existing = map.get(sessionId);
      const next = new Map(existing ?? []);
      for (const toolUseId of event.toolUseIds) {
        next.set(toolUseId, {
          ...next.get(toolUseId),
          summary,
          batchId: event.batchId,
        });
      }
      return new Map(map).set(sessionId, next);
    });
  }

  /**
   * Seed summaries persisted server-side, replayed on the `GET /messages`
   * `toolSummaries` sidecar at conversation load.
   *
   * Non-clobbering by `toolUseId`, matching `McpAppStateService.seedFromHydration`
   * and `ArtifactStateService.seedFromHydration`: a slow hydration response
   * must never undo a live summary that arrived while it was in flight.
   */
  seedFromHydration(
    sessionId: string,
    rows: readonly { batchId?: string; toolUseIds?: string[]; summary?: string }[],
  ): void {
    if (!rows.length) return;

    this.bySession.update(map => {
      const existing = map.get(sessionId);
      const next = new Map(existing ?? []);
      let changed = false;
      for (const row of rows) {
        const summary = row.summary?.trim();
        if (!summary || !row.toolUseIds?.length) continue;
        for (const toolUseId of row.toolUseIds) {
          if (next.get(toolUseId)?.summary) continue;
          next.set(toolUseId, {
            ...next.get(toolUseId),
            summary,
            batchId: row.batchId,
          });
          changed = true;
        }
      }
      if (!changed) return map;
      return new Map(map).set(sessionId, next);
    });
  }

  // -- internals -----------------------------------------------------------

  private mergeInsight(
    sessionId: string,
    toolUseId: string,
    patch: Partial<ToolInsight>,
  ): void {
    this.bySession.update(map => {
      const existing = map.get(sessionId);
      const next = new Map(existing ?? []);
      next.set(toolUseId, { ...next.get(toolUseId), ...patch });
      return new Map(map).set(sessionId, next);
    });
  }
}
