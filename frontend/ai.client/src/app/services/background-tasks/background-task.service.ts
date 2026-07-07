import { Injectable, signal } from '@angular/core';

/** Lifecycle of a tracked background task. */
export type BackgroundTaskStatus = 'processing' | 'completed' | 'error';

/**
 * A long-running, out-of-band process surfaced to the user as a persistent
 * corner toast (e.g. a headless "Run now", an export, an ingestion job). The
 * toast lives at the app-shell level so it survives navigation between views.
 */
export interface BackgroundTask {
  id: string;
  /** Short headline, e.g. `Running "Morning Briefing"`. */
  title: string;
  /** Optional one-line status detail (e.g. "The agent is working…"). */
  detail?: string | null;
  status: BackgroundTaskStatus;
  /**
   * Router commands for a "View" action. When set, the toast shows a button
   * that navigates here — e.g. `['/s', sessionId]` to open a headless run's
   * conversation in the session-detail view. Reusable for any result target.
   */
  route?: string[] | null;
  /** Label for the view button; defaults to "View". */
  viewLabel?: string;
  /**
   * Optional custom handler for the "View" button. When set, the toast runs
   * this instead of navigating to `route` — lets a consumer do side work
   * first (e.g. optimistically seed the session header title before routing,
   * mirroring a sidebar click). `route` is still used to *show* the button.
   */
  onView?: () => void;
  createdAt: number;
}

/**
 * Tracks background tasks and exposes them as a signal for a shell-level toast
 * stack. Deliberately generic — "Run now" is the first consumer, but exports,
 * ingestion jobs, and other headless work can register here too.
 *
 * A root service (not a component) owns task lifecycle so the tracking — and
 * any subscription that resolves it — survives the originating component being
 * destroyed on navigation.
 */
@Injectable({ providedIn: 'root' })
export class BackgroundTaskService {
  private tasksSignal = signal<BackgroundTask[]>([]);

  /** Readonly signal of the current tasks, oldest first. */
  readonly tasks = this.tasksSignal.asReadonly();

  /** Register a new in-progress task; returns its id for later resolution. */
  start(title: string, detail?: string): string {
    const id = this.generateId();
    this.tasksSignal.update((tasks) => [
      ...tasks,
      { id, title, detail: detail ?? null, status: 'processing', route: null, createdAt: Date.now() },
    ]);
    return id;
  }

  /** Mark a task finished, optionally attaching a "View" route + detail. */
  complete(
    id: string,
    patch: { detail?: string; route?: string[] | null; viewLabel?: string; onView?: () => void } = {},
  ): void {
    this.patch(id, { status: 'completed', ...patch });
  }

  /** Mark a task failed. A route may still be attached (a failed run can
   *  materialize a partial session worth viewing). */
  fail(id: string, detail: string, route?: string[] | null): void {
    this.patch(id, { status: 'error', detail, route: route ?? null });
  }

  /** Remove a task from the stack. */
  dismiss(id: string): void {
    this.tasksSignal.update((tasks) => tasks.filter((t) => t.id !== id));
  }

  private patch(id: string, changes: Partial<BackgroundTask>): void {
    this.tasksSignal.update((tasks) => tasks.map((t) => (t.id === id ? { ...t, ...changes } : t)));
  }

  private generateId(): string {
    return `bgtask-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }
}
