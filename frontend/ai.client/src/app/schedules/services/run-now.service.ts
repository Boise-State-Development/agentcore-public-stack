import { Injectable, inject } from '@angular/core';
import { Router } from '@angular/router';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import { SessionService as SessionListService } from '../../session/services/session/session.service';
import { RunNowRequest, RunNowResponse } from '../models/schedule.model';
import { RunApiService } from './run-api.service';

/**
 * Fires a headless "Run now" and tracks it as a persistent background task.
 *
 * Fire-and-forget from the caller's view: `run()` returns immediately and the
 * corner toast reports progress. The `/runs/now` request is subscribed here in
 * a root service (not the component) so it — and the toast resolution — survive
 * the originating form being destroyed on navigation. When the run finishes,
 * the task gets a "View" route to the session-detail conversation, where the
 * result renders with full formatting.
 */
@Injectable({ providedIn: 'root' })
export class RunNowService {
  private readonly runApi = inject(RunApiService);
  private readonly tasks = inject(BackgroundTaskService);
  private readonly sessions = inject(SessionListService);
  private readonly router = inject(Router);

  /** Start a headless run in the background. Returns the task id. */
  run(request: RunNowRequest): string {
    const label = request.title?.trim() || 'your prompt';
    const taskId = this.tasks.start(`Running "${label}"`, 'The agent is working…');

    this.runApi.runNow(request).subscribe({
      next: (result) => {
        const route = result.sessionId ? ['/s', result.sessionId] : null;
        // The run materializes as a real session server-side. Refresh the
        // sidebar list so it appears like any other conversation (with its
        // server-generated title + unread dot), not just behind the toast link.
        if (result.sessionId) {
          this.sessions.refreshSessions();
        }
        if (result.status === 'completed') {
          this.tasks.complete(taskId, {
            detail: 'Run finished — added to your conversations',
            route,
            viewLabel: 'View result',
            onView: () => this.openResult(result),
          });
        } else if (result.status === 'oauth_required') {
          this.tasks.fail(taskId, 'A tool needs you to connect an account first.', route);
        } else {
          this.tasks.fail(taskId, result.error ?? `Run ${result.status}.`, route);
        }
      },
      error: (err: unknown) => {
        const message = err instanceof Error ? err.message : 'Run failed.';
        this.tasks.fail(taskId, message);
      },
    });

    return taskId;
  }

  /**
   * Open a finished run's conversation. Optimistically seeds `currentSession`
   * with the id + known title *before* routing so the session-detail top-nav
   * shows the right title immediately, instead of lingering on the previously
   * viewed conversation's title until the metadata fetch resolves. The
   * ConversationPage's metadata load then reconciles the full record. Mirrors
   * `SessionListComponent.onSessionClick`.
   */
  private openResult(result: RunNowResponse): void {
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
