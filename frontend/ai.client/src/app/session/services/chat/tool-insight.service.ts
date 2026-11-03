import { Injectable, signal } from '@angular/core';
import type {
  AgentStatusEvent,
  ToolGroupSummaryEvent,
  SourceProvenanceEvent,
  SourceProvenanceClaim,
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
  /**
   * Source-provenance verdicts, when this call was a `verify_sources`.
   *
   * Stored per call rather than per message because the verdicts belong to a
   * specific invocation: a turn can verify twice against two documents, and
   * merging them would attribute a passage to the wrong file.
   */
  provenance?: readonly SourceProvenanceClaim[];
}

/** One tool the runtime has started and not yet reported finishing. */
export interface RunningTool {
  toolUseId: string;
  toolName: string;
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

  /**
   * Epoch ms at which each conversation's current turn began.
   *
   * Recorded at the stream reset rather than at the first `agent_status`,
   * because the wait the user is actually measuring starts when they hit
   * send — the gap before the first runtime event is often the longest part
   * of it, and a timer that only starts once the model replies would hide
   * exactly the stall worth seeing.
   */
  private readonly turnStartBySession = signal<ReadonlyMap<string, number>>(
    new Map(),
  );

  /**
   * Tools the runtime has started and not yet reported finishing, per
   * conversation, in the order they started.
   *
   * Why this rather than the content stream: a `toolUse` block starts
   * streaming when the model begins writing the tool's ARGUMENTS, while
   * `tool_start` fires when the tool begins EXECUTING. Measured on dev, the
   * gap was ~640ms in which the content stream already claimed a tool was
   * running and nothing was. This set is the truthful source.
   *
   * Only trustworthy because the drain now runs concurrently with the agent
   * stream (docs/specs/agent-state-feedback.md PR-2). Before that a
   * `tool_start` arrived bundled with its own `tool_end` and this set would
   * have been empty for the entire time the tools were running.
   *
   * It is a LIST, not a single value, although the agent pins
   * `SequentialToolExecutor` today and so never runs two tools at once. The
   * shape costs nothing and is what a concurrent executor would need; the
   * "and N more" readout built on it was removed as dead code, see
   * `message-list.component.ts`.
   */
  private readonly runningBySession = signal<
    ReadonlyMap<string, readonly RunningTool[]>
  >(new Map());

  /**
   * The most recent batch summary each conversation received, live or hydrated.
   *
   * What a handoff row shows as its status line while its target runs in the
   * background (handoffs spec §4.3): the latest thing the Agent did, in the
   * same words the target's own tool rail uses.
   */
  private readonly latestSummaryBySession = signal<ReadonlyMap<string, string>>(new Map());

  // -- reads ---------------------------------------------------------------

  /** The latest tool-batch summary a conversation received, if any. */
  latestSummary(sessionId: string): string | undefined {
    return this.latestSummaryBySession().get(sessionId);
  }

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

  /**
   * Provenance verdicts for one `verify_sources` call, if it carried any.
   *
   * Unlike `summaryFor`, this takes a single id rather than a group: a batch
   * summary describes a group of calls, while a verdict describes exactly one
   * call's claims, and widening the lookup to the group would let a second
   * verification in the same batch answer for the first.
   */
  provenanceFor(
    sessionId: string,
    toolUseId: string,
  ): readonly SourceProvenanceClaim[] | undefined {
    return this.bySession().get(sessionId)?.get(toolUseId)?.provenance;
  }

  /**
   * Tools in flight for a conversation, oldest first. Empty when the model is
   * generating rather than calling tools.
   */
  runningTools(sessionId: string): readonly RunningTool[] {
    return this.runningBySession().get(sessionId) ?? [];
  }

  /** The live status of a conversation's streaming turn, or undefined. */
  status(sessionId: string): AgentStatusEvent | undefined {
    return this.statusBySession().get(sessionId);
  }

  /** Epoch ms the current turn started, or undefined when none is running. */
  turnStartedAt(sessionId: string): number | undefined {
    return this.turnStartBySession().get(sessionId);
  }

  // -- writes --------------------------------------------------------------

  /**
   * Apply one `agent_status` transition.
   *
   * `tool_end` is the only phase that carries a durable fact (the duration),
   * so it writes through to the insight registry as well as updating the live
   * status. The rest are live-only.
   */
  /** Mark the start of a turn, for the elapsed-time readout. */
  startTurn(sessionId: string): void {
    this.turnStartBySession.update(map => new Map(map).set(sessionId, Date.now()));
  }

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

    this.updateRunning(sessionId, event);
  }

  /**
   * Open or close this conversation's set of in-flight tools.
   *
   * `thinking` clears it rather than leaving it alone: that phase means the
   * model is generating, which can only happen once the batch before it
   * finished. It is the backstop for a `tool_end` that never arrived — a tool
   * that raised past the hook, or a batch cut short by an interrupt — because
   * a tool stuck in this set forever would have the loader naming a tool that
   * stopped running minutes ago.
   */
  private updateRunning(sessionId: string, event: AgentStatusEvent): void {
    if (event.phase === 'thinking') {
      this.runningBySession.update(map => {
        if (!map.get(sessionId)?.length) return map;
        return new Map(map).set(sessionId, []);
      });
      return;
    }

    if (!event.toolUseId || !event.toolName) return;
    const { toolUseId, toolName } = event;

    this.runningBySession.update(map => {
      const current = map.get(sessionId) ?? [];

      if (event.phase === 'tool_start') {
        // Re-delivery of a start we already hold is a no-op, not a duplicate
        // row — the count drives "and 2 more".
        if (current.some(t => t.toolUseId === toolUseId)) return map;
        return new Map(map).set(sessionId, [...current, { toolUseId, toolName }]);
      }

      const next = current.filter(t => t.toolUseId !== toolUseId);
      if (next.length === current.length) return map;
      return new Map(map).set(sessionId, next);
    });
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
    // In-flight tools go with it. A turn that ended — normally, by Stop, or by
    // an error — has nothing running, whatever the last transition claimed.
    this.runningBySession.update(map => {
      if (!map.has(sessionId)) return map;
      const next = new Map(map);
      next.delete(sessionId);
      return next;
    });
    // The turn clock is deliberately NOT cleared here. `clearStatus` fires on
    // the turn's falling edge while the loader is still mounted for a frame or
    // two, and dropping the start time mid-teardown made the elapsed readout
    // vanish before the loader did — a visible flicker on every single turn.
    // It is overwritten by the next `startTurn`, and read only while a turn is
    // running, so leaving it costs nothing.
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
    this.latestSummaryBySession.update(map => new Map(map).set(sessionId, summary));

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
  /** Record one `verify_sources` call's verdicts (live `source_provenance`). */
  recordProvenance(sessionId: string, event: SourceProvenanceEvent): void {
    if (!event.toolUseId || !event.claims?.length) return;

    this.bySession.update(map => {
      const next = new Map(map.get(sessionId) ?? []);
      next.set(event.toolUseId, {
        ...next.get(event.toolUseId),
        provenance: event.claims,
      });
      return new Map(map).set(sessionId, next);
    });
  }

  /**
   * Seed provenance persisted server-side, replayed on the `GET /messages`
   * `sourceProvenance` sidecar at conversation load.
   *
   * Non-clobbering by `toolUseId`, for the same reason as the summaries above:
   * a slow hydration response must never undo a live verdict. It matters more
   * here — overwriting a live `quote_unsupported` with a stale row would
   * replace a warning the user is looking at.
   */
  seedProvenanceFromHydration(
    sessionId: string,
    rows: readonly { toolUseId?: string; claims?: SourceProvenanceClaim[] }[],
  ): void {
    if (!rows.length) return;

    this.bySession.update(map => {
      const next = new Map(map.get(sessionId) ?? []);
      let changed = false;
      for (const row of rows) {
        if (!row.toolUseId || !row.claims?.length) continue;
        if (next.get(row.toolUseId)?.provenance) continue;
        next.set(row.toolUseId, {
          ...next.get(row.toolUseId),
          provenance: row.claims,
        });
        changed = true;
      }
      if (!changed) return map;
      return new Map(map).set(sessionId, next);
    });
  }

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
