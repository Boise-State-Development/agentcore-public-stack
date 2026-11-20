import { Injectable, signal } from '@angular/core';

import { RunReview, WorkflowRunView } from './workflow-run.models';

/**
 * What the SPA knows about workflow runs, per source conversation (docs/specs/workflow-runs.md).
 *
 * State only, with no HTTP or chat dependencies, so `MessageMapService` can seed it from
 * `GET /messages` without an injection cycle, as `HandoffStateService` is seeded. The actions live
 * in `WorkflowRunService` and `WorkflowRunQueueService`.
 *
 * - **reviews**, by source session: the open run sheet, at most one per thread.
 * - **runs**, by source session: the runs `workflowRuns` replayed, so a reload can find an approved
 *   run whose steps were never started.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunStateService {
  private readonly reviewsBySource = signal<ReadonlyMap<string, RunReview>>(new Map());
  private readonly runsBySource = signal<ReadonlyMap<string, readonly WorkflowRunView[]>>(new Map());

  reviewFor(sourceSessionId: string | null | undefined): RunReview | undefined {
    if (!sourceSessionId) return undefined;
    return this.reviewsBySource().get(sourceSessionId);
  }

  setReview(sourceSessionId: string, review: RunReview): void {
    this.reviewsBySource.update((map) => new Map(map).set(sourceSessionId, review));
  }

  updateReview(sourceSessionId: string, patch: Partial<RunReview>): void {
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

  runsFor(sourceSessionId: string | null | undefined): readonly WorkflowRunView[] {
    if (!sourceSessionId) return [];
    return this.runsBySource().get(sourceSessionId) ?? [];
  }

  /** Seed from a `GET /messages` response. Entries that aren't run-shaped are skipped. */
  hydrate(sessionId: string, runs: readonly unknown[] | undefined): void {
    const valid = (runs ?? []).filter(isRunView);
    this.runsBySource.update((map) => {
      if (valid.length === 0 && !map.has(sessionId)) return map;
      return new Map(map).set(sessionId, valid);
    });
  }

  /** Replace one run (after an approve in this tab). */
  upsertRun(run: WorkflowRunView): void {
    this.runsBySource.update((map) => {
      const runs = map.get(run.sessionId) ?? [];
      const index = runs.findIndex((existing) => existing.runId === run.runId);
      const next = index < 0 ? [...runs, run] : runs.map((existing, i) => (i === index ? run : existing));
      return new Map(map).set(run.sessionId, next);
    });
  }
}

function isRunView(value: unknown): value is WorkflowRunView {
  if (!value || typeof value !== 'object') return false;
  const run = value as Partial<WorkflowRunView>;
  return typeof run.runId === 'string' && typeof run.sessionId === 'string' && Array.isArray(run.steps);
}
