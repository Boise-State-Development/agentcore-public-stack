import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import { AppNotification, NotificationResolution } from '../../services/notifications/notification.model';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { DECISION_WORDS } from '../../services/notifications/needs-user';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ToastService } from '../../services/toast/toast.service';
import { SessionService as SessionListService } from '../../session/services/session/session.service';
import {
  InboxReviewDialogComponent,
  InboxReviewDialogData,
  InboxReviewDialogResult,
} from '../components/inbox-review-dialog/inbox-review-dialog.component';
import { RunApiService } from './run-api.service';
import { RunOutcome, pollRunUntilDone } from './run-now.service';

/**
 * Answers a paused scheduled run from the bell (answer-from-inbox §3.9).
 *
 * `review` opens the review dialog. Once it sends, the 202's `resumeRunId` goes to the same
 * background-task tracker Run now uses: a corner toast that polls `GET /runs/{runId}` and says
 * how it ended. No bell row is written for the outcome (settled question 4). The bell re-reads
 * when the tracker settles, so the row shows what the worker recorded.
 *
 * A root service, so the tracking outlives the dialog and whatever page opened it.
 */
@Injectable({ providedIn: 'root' })
export class InboxAnswerService {
  private readonly dialog = inject(Dialog);
  private readonly runApi = inject(RunApiService);
  private readonly tasks = inject(BackgroundTaskService);
  private readonly notifications = inject(NotificationsService);
  private readonly sessions = inject(SessionListService);
  private readonly sidenav = inject(SidenavService);
  private readonly router = inject(Router);
  private readonly toast = inject(ToastService);

  private readonly settledRuns = signal(0);

  /** Bumped each time a resume this tab started settles, so a page showing its schedule re-reads. */
  readonly settledCount = this.settledRuns.asReadonly();

  /** Open the review dialog for a waiting row, and act on how it closed. */
  async review(notification: AppNotification): Promise<void> {
    const ref = this.dialog.open<InboxReviewDialogResult, InboxReviewDialogData>(InboxReviewDialogComponent, {
      data: { notification },
    });
    const result = await firstValueFrom(ref.closed);
    if (!result) return;
    switch (result.kind) {
      case 'sent':
        this.track(notification, result.resumeRunId, result.resolution);
        return;
      case 'settled':
        this.notifications.settled(notification.notificationId, {
          state: result.state,
          at: new Date().toISOString(),
        });
        this.toast.info(result.state === 'expired' ? 'Expired' : 'Already answered', result.message);
        void this.notifications.refresh();
        return;
      case 'open':
        this.openConversation(notification);
        return;
    }
  }

  /** Open the paused run's conversation, where every kind of prompt can be answered. */
  openConversation(notification: AppNotification): void {
    const sessionId = notification.payload?.sessionId;
    if (typeof sessionId === 'string' && sessionId) {
      void this.router.navigate(['/s', sessionId]);
    } else {
      void this.router.navigate(['/schedules']);
    }
    this.sidenav.close();
  }

  private track(notification: AppNotification, resumeRunId: string, resolution: NotificationResolution): void {
    const label = notification.payload?.label?.trim() || 'your scheduled run';
    const decision = DECISION_WORDS[resolution.decision ?? ''] ?? 'Answered';
    const taskId = this.tasks.start(`${decision}: “${label}”`, 'The run is continuing…');
    pollRunUntilDone(this.runApi, resumeRunId).subscribe({
      next: (outcome) => void this.finish(taskId, notification, decision, outcome),
      error: () => this.tasks.fail(taskId, 'Couldn’t follow the run. Check its conversation for the result.'),
    });
  }

  private async finish(
    taskId: string,
    notification: AppNotification,
    decision: string,
    outcome: RunOutcome,
  ): Promise<void> {
    const sessionId = notification.payload?.sessionId;
    const route = typeof sessionId === 'string' && sessionId ? ['/s', sessionId] : null;
    this.sessions.refreshSessions();
    await this.notifications.refresh();
    this.settledRuns.update((n) => n + 1);
    // The worker settles the row just after the run ends. When the re-read already has it, it
    // says why a run stopped better than the run's own error; when it doesn't, the error stands.
    const row = this.notifications.notifications().find((n) => n.notificationId === notification.notificationId);
    const state = row?.resolution?.state;

    if (outcome === null) {
      this.tasks.fail(taskId, 'The run didn’t report back. Check its conversation for the result.', route);
    } else if (outcome.status === 'completed') {
      this.tasks.complete(taskId, { detail: `${decision} · finished`, route, viewLabel: 'View result' });
    } else if (outcome.status === 'needs_user') {
      this.tasks.complete(taskId, { detail: 'It’s waiting for you again', route, viewLabel: 'Open' });
    } else if (outcome.status === 'oauth_required') {
      this.tasks.fail(taskId, 'It needs you to connect an account. Open the conversation to connect.', route);
    } else if (state === 'retry') {
      // The lease was held (the conversation was mid-turn) or the run couldn't start: nothing
      // was resumed, and the row's buttons are back.
      this.tasks.fail(taskId, 'It couldn’t continue just now, so nothing changed. Answer again from the bell.', route);
    } else if (state === 'answered_elsewhere') {
      this.tasks.fail(taskId, 'It was already answered in the conversation.', route);
    } else if (state === 'expired') {
      this.tasks.fail(taskId, 'It expired before it could continue.', route);
    } else {
      this.tasks.fail(taskId, outcome.error || 'The run stopped with an error.', route);
    }
  }
}
