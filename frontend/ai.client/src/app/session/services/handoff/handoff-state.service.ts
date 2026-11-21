import { Injectable, signal } from '@angular/core';
import { HandoffReview, HandoffView, SpawnedFrom } from './handoff.models';

/**
 * What the SPA knows about handoffs, per conversation (handoffs spec §4.2–§4.3).
 *
 * State only, with no HTTP or chat dependencies, so `MessageMapService` can seed it
 * from `GET /messages` and the message list can read it without an injection cycle
 * through `ChatRequestService`. The actions live in `AgentHandoffService`.
 *
 * - **rows**, by source session: the handoffs a thread sent, each rendered as a handoff
 *   row after its `sourceMessageIndex`.
 * - **spawnedFrom**, by target session: where a conversation's context came from, for
 *   the "Context from *{title}*" label on its first bubble.
 * - **reviews**, by source session: the open review sheet, at most one per thread.
 * - **focus**: the row a notification asked to bring into view (PR-0.3c). It waits here until that
 *   row renders, which may be after the source's messages load.
 *
 * Everything is keyed by session id, like the rest of the chat stack, so a handoff
 * running in the background never touches the state of the thread being viewed.
 */
@Injectable({ providedIn: 'root' })
export class HandoffStateService {
  private readonly rowsBySource = signal<ReadonlyMap<string, readonly HandoffView[]>>(new Map());
  private readonly spawnedByTarget = signal<ReadonlyMap<string, SpawnedFrom>>(new Map());
  private readonly reviewsBySource = signal<ReadonlyMap<string, HandoffReview>>(new Map());
  private readonly focusRequest = signal<string | null>(null);

  /** The handoff whose row should scroll into view and take focus, once. */
  readonly focusedHandoffId = this.focusRequest.asReadonly();

  requestFocus(handoffId: string): void {
    this.focusRequest.set(handoffId);
  }

  /** The row took the focus it was asked to. */
  clearFocus(handoffId: string): void {
    if (this.focusRequest() === handoffId) this.focusRequest.set(null);
  }

  rowsFor(sourceSessionId: string | null | undefined): readonly HandoffView[] {
    if (!sourceSessionId) return [];
    return this.rowsBySource().get(sourceSessionId) ?? [];
  }

  /** Every row the SPA holds, for the completion watcher and the unload guard. */
  allRows(): readonly HandoffView[] {
    return [...this.rowsBySource().values()].flat();
  }

  spawnedFrom(targetSessionId: string | null | undefined): SpawnedFrom | undefined {
    if (!targetSessionId) return undefined;
    return this.spawnedByTarget().get(targetSessionId);
  }

  reviewFor(sourceSessionId: string | null | undefined): HandoffReview | undefined {
    if (!sourceSessionId) return undefined;
    return this.reviewsBySource().get(sourceSessionId);
  }

  /**
   * Seed from a `GET /messages` response. Replaces the server's view of a source's rows but keeps
   * a row the server doesn't list yet (sent in this tab a moment ago), so a reload racing a send
   * can't make a fresh row vanish.
   */
  hydrate(
    sessionId: string,
    handoffs: readonly HandoffView[] | undefined,
    spawnedFrom: SpawnedFrom | null | undefined,
  ): void {
    const server = handoffs ?? [];
    this.rowsBySource.update((map) => {
      const local = map.get(sessionId) ?? [];
      const known = new Set(server.map((row) => row.handoffId));
      const merged = [...server, ...local.filter((row) => !known.has(row.handoffId))];
      if (merged.length === 0 && !map.has(sessionId)) return map;
      return new Map(map).set(sessionId, merged);
    });
    if (spawnedFrom) {
      this.spawnedByTarget.update((map) => new Map(map).set(sessionId, spawnedFrom));
    }
  }

  /**
   * Take rows read from the server since, never moving a row backwards: a row this tab doesn't
   * hold is added, and one it holds is replaced only by a copy written later (`updatedAt`). A row
   * `isLive` says this tab is streaming is left alone, since its stream is newer than any read.
   * For a run's steps re-read after a reload, when the tab's copy still says "running" for a step
   * that finished on the server (`WorkflowRunRefreshService`). Returns how many rows moved.
   */
  advanceRows(rows: readonly HandoffView[], isLive: (row: HandoffView) => boolean = () => false): number {
    let moved = 0;
    this.rowsBySource.update((map) => {
      let next: Map<string, readonly HandoffView[]> | null = null;
      for (const row of rows) {
        const held = (next ?? map).get(row.sessionId) ?? [];
        const index = held.findIndex((existing) => existing.handoffId === row.handoffId);
        if (index >= 0) {
          const current = held[index];
          if (isLive(current) || !isNewer(row, current)) continue;
        }
        next ??= new Map(map);
        next.set(row.sessionId, index < 0 ? [...held, row] : held.map((existing, i) => (i === index ? row : existing)));
        moved += 1;
      }
      return next ?? map;
    });
    return moved;
  }

  /** Add or replace one row (a send in this tab, or a refreshed status). */
  upsertRow(row: HandoffView): void {
    this.rowsBySource.update((map) => {
      const rows = map.get(row.sessionId) ?? [];
      const index = rows.findIndex((existing) => existing.handoffId === row.handoffId);
      const next = index < 0 ? [...rows, row] : rows.map((existing, i) => (i === index ? row : existing));
      return new Map(map).set(row.sessionId, next);
    });
  }

  setSpawnedFrom(targetSessionId: string, spawnedFrom: SpawnedFrom): void {
    this.spawnedByTarget.update((map) => new Map(map).set(targetSessionId, spawnedFrom));
  }

  setReview(sourceSessionId: string, review: HandoffReview): void {
    this.reviewsBySource.update((map) => new Map(map).set(sourceSessionId, review));
  }

  updateReview(sourceSessionId: string, patch: Partial<HandoffReview>): void {
    const current = this.reviewsBySource().get(sourceSessionId);
    if (!current) return;
    this.setReview(sourceSessionId, { ...current, ...patch });
  }

  clearReview(sourceSessionId: string): void {
    this.reviewsBySource.update((map) => {
      if (!map.has(sourceSessionId)) return map;
      const next = new Map(map);
      next.delete(sourceSessionId);
      return next;
    });
  }
}

/** `candidate` was written after `current`. A time that can't be read never counts as newer. */
function isNewer(candidate: HandoffView, current: HandoffView): boolean {
  const next = Date.parse(candidate.updatedAt);
  const held = Date.parse(current.updatedAt);
  if (Number.isNaN(next)) return false;
  return Number.isNaN(held) || next > held;
}
