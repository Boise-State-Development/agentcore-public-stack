import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, catchError, exhaustMap, filter, finalize, map, of, switchMap, take, timer } from 'rxjs';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import { SessionService as SessionListService } from '../../session/services/session/session.service';
import {
  RunNowRequest,
  RunStartedResponse,
  RunStatusResponse,
  TERMINAL_RUN_STATUSES,
} from '../models/schedule.model';
import { RunApiService } from './run-api.service';

/** How often to ask app-api whether a run has finished. */
export const RUN_POLL_INTERVAL_MS = 3000;

/**
 * When a still-`started` run counts as lost. The harness stops a run at 14
 * minutes (`DEFAULT_MAX_DURATION_SECONDS` in `harness/runner.py`), so a run
 * with no outcome after 16 minutes died with its app-api task (a deploy or
 * restart) and will never write one.
 */
export const RUN_LOST_AFTER_MS = 16 * 60 * 1000;

const TERMINAL = new Set<string>(TERMINAL_RUN_STATUSES);

/** A poll's result: the finished run, or `null` when it was given up as lost. */
export type RunOutcome = RunStatusResponse | null;

/**
 * Poll `GET /runs/{runId}` until the run ends or counts as lost. A failed poll is skipped, not
 * fatal: the run is still going on the server either way. The lost clock starts now, on this
 * browser's clock, so server clock skew can't end a run early. Run now and an answer from the
 * inbox (answer-from-inbox §3.9) both track a run with it.
 */
export function pollRunUntilDone(runApi: RunApiService, runId: string): Observable<RunOutcome> {
  const startedAt = Date.now();
  return timer(RUN_POLL_INTERVAL_MS, RUN_POLL_INTERVAL_MS).pipe(
    exhaustMap(() => runApi.getRun(runId).pipe(catchError(() => of(undefined)))),
    map((status): RunOutcome | undefined => {
      if (status && TERMINAL.has(status.status)) {
        return status;
      }
      return Date.now() - startedAt >= RUN_LOST_AFTER_MS ? null : undefined;
    }),
    filter((outcome): outcome is RunOutcome => outcome !== undefined),
    take(1),
  );
}

/**
 * Fires a headless "Run now" and tracks it as a persistent background task.
 *
 * Fire-and-forget from the caller's view: `run()` returns immediately and the
 * corner toast reports progress. POST `/runs/now` answers as soon as the run
 * starts, so a turn longer than the 60s CloudFront timeout still reports its
 * real outcome: this service polls GET `/runs/{runId}` until the run ends.
 * The request and the polling live in this root service (not the component)
 * so they — and the toast resolution — survive the originating form being
 * destroyed on navigation. When the run finishes, the task gets a "View"
 * route to the session-detail conversation, where the result renders with
 * full formatting.
 */
@Injectable({ providedIn: 'root' })
export class RunNowService {
  private readonly runApi = inject(RunApiService);
  private readonly tasks = inject(BackgroundTaskService);
  private readonly sessions = inject(SessionListService);
  private readonly router = inject(Router);

  private readonly inFlight = signal(0);

  /** True while a run started from this tab hasn't finished. */
  readonly running = computed(() => this.inFlight() > 0);

  /** Start a headless run in the background. Returns the task id. */
  run(request: RunNowRequest): string {
    const label = request.title?.trim() || 'your prompt';
    const taskId = this.tasks.start(`Running "${label}"`, 'The agent is working…');
    this.inFlight.update((n) => n + 1);

    let started: RunStartedResponse | null = null;
    this.runApi
      .runNow(request)
      .pipe(
        switchMap((response) => {
          started = response;
          return pollRunUntilDone(this.runApi, response.runId);
        }),
        finalize(() => this.inFlight.update((n) => n - 1)),
      )
      .subscribe({
        next: (outcome) => this.resolve(taskId, started!, outcome),
        error: (err: unknown) => {
          const message = err instanceof Error ? err.message : 'Run failed.';
          this.tasks.fail(taskId, message);
        },
      });

    return taskId;
  }

  private resolve(taskId: string, started: RunStartedResponse, result: RunOutcome): void {
    const route = ['/s', started.sessionId];
    // The run materializes as a real session server-side. Refresh the
    // sidebar list so it appears like any other conversation (with its
    // server-generated title + unread dot), not just behind the toast link.
    this.sessions.refreshSessions();

    if (result === null) {
      this.tasks.fail(
        taskId,
        "The run didn't report back. Check your conversations for its result.",
        route,
      );
    } else if (result.status === 'completed') {
      this.tasks.complete(taskId, {
        detail: 'Run finished — added to your conversations',
        route,
        viewLabel: 'View result',
        onView: () => this.openResult(result),
      });
    } else if (result.status === 'needs_user') {
      // The run paused on an approval, a question, a form or a sign-in: the
      // conversation holds the card to answer.
      this.tasks.complete(taskId, {
        detail: 'Waiting for your answer',
        route,
        viewLabel: 'Open',
        onView: () => this.openResult(result),
      });
    } else if (result.status === 'oauth_required') {
      this.tasks.fail(taskId, 'A tool needs you to connect an account first.', route);
    } else {
      this.tasks.fail(taskId, result.error ?? `Run ${result.status}.`, route);
    }
  }

  /**
   * Open a finished run's conversation. Optimistically seeds `currentSession`
   * with the id + known title *before* routing so the session-detail top-nav
   * shows the right title immediately, instead of lingering on the previously
   * viewed conversation's title until the metadata fetch resolves. The
   * ConversationPage's metadata load then reconciles the full record. Mirrors
   * `SessionListComponent.onSessionClick`.
   */
  private openResult(result: RunStatusResponse): void {
    // Minimal optimistic record — the ConversationPage metadata fetch fills in
    // the authoritative timestamps/counts moments later.
    this.sessions.currentSession.set({
      sessionId: result.sessionId,
      userId: '',
      title: result.title ?? '',
      status: 'active',
      createdAt: '',
      lastMessageAt: '',
      messageCount: 0,
    });
    this.router.navigate(['/s', result.sessionId]);
  }
}
