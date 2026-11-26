import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { InboxAnswerService } from './inbox-answer.service';
import { RUN_POLL_INTERVAL_MS } from './run-now.service';
import { RunApiService } from './run-api.service';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { AppNotification, NotificationResolution } from '../../services/notifications/notification.model';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ToastService } from '../../services/toast/toast.service';
import { SessionService as SessionListService } from '../../session/services/session/session.service';
import { InboxReviewDialogResult } from '../components/inbox-review-dialog/inbox-review-dialog.component';
import { RunStatusResponse } from '../models/schedule.model';

const ROW: AppNotification = {
  notificationId: 'n1',
  recipientEmail: 'me@x.edu',
  kind: 'agent_needs_approval',
  payload: { sessionId: 'headless-7', label: 'Inbox watcher', answerable: true, prompts: [] },
  createdAt: '2026-11-26T00:00:00Z',
  readAt: null,
};

const APPLYING: NotificationResolution = { state: 'applying', decision: 'approved', at: '2026-11-26T12:00:00Z', resumeRunId: 'r-1' };

function outcome(over: Partial<RunStatusResponse>): RunStatusResponse {
  return { runId: 'r-1', sessionId: 'headless-7', status: 'completed', startedAt: 'x', ...over };
}

describe('InboxAnswerService', () => {
  let service: InboxAnswerService;
  let tasks: BackgroundTaskService;
  const closed = { value: undefined as InboxReviewDialogResult | undefined };
  const dialog = { open: vi.fn(() => ({ closed: of(closed.value) })) };
  const runApi = { getRun: vi.fn() };
  const inbox = signal<AppNotification[]>([]);
  const notifications = { notifications: inbox, refresh: vi.fn().mockResolvedValue(undefined), settled: vi.fn() };
  const sessions = { refreshSessions: vi.fn() };
  const sidenav = { close: vi.fn() };
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  const toast = { info: vi.fn() };

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    inbox.set([]);
    TestBed.configureTestingModule({
      providers: [
        { provide: Dialog, useValue: dialog },
        { provide: RunApiService, useValue: runApi },
        { provide: NotificationsService, useValue: notifications },
        { provide: SessionListService, useValue: sessions },
        { provide: SidenavService, useValue: sidenav },
        { provide: Router, useValue: router },
        { provide: ToastService, useValue: toast },
      ],
    });
    service = TestBed.inject(InboxAnswerService);
    tasks = TestBed.inject(BackgroundTaskService);
  });

  afterEach(() => vi.useRealTimers());

  async function sendAndSettle(result: RunStatusResponse, row?: AppNotification): Promise<void> {
    closed.value = { kind: 'sent', resumeRunId: 'r-1', resolution: APPLYING };
    runApi.getRun.mockReturnValue(of(result));
    notifications.refresh.mockImplementation(async () => {
      if (row) inbox.set([row]);
    });
    await service.review(ROW);
    expect(tasks.tasks()[0]).toMatchObject({ title: 'Approved: “Inbox watcher”', status: 'processing' });
    await vi.advanceTimersByTimeAsync(RUN_POLL_INTERVAL_MS);
  }

  it('tracks the resume like Run now, toasts how it ended, and re-reads the bell', async () => {
    const before = service.settledCount();
    await sendAndSettle(outcome({ status: 'completed' }));
    expect(runApi.getRun).toHaveBeenCalledWith('r-1');
    expect(tasks.tasks()[0]).toMatchObject({
      status: 'completed',
      detail: 'Approved · finished',
      route: ['/s', 'headless-7'],
      viewLabel: 'View result',
    });
    expect(notifications.refresh).toHaveBeenCalled();
    expect(sessions.refreshSessions).toHaveBeenCalled();
    expect(service.settledCount()).toBe(before + 1);
  });

  it('says when the run is waiting again', async () => {
    await sendAndSettle(outcome({ status: 'needs_user' }));
    expect(tasks.tasks()[0]).toMatchObject({ status: 'completed', detail: 'It’s waiting for you again' });
  });

  it('a run the worker gave back reads as nothing changed, with the buttons back', async () => {
    const given = { ...ROW, resolution: { state: 'retry' as const, at: 'x' } };
    await sendAndSettle(outcome({ status: 'error', error: 'conflict' }), given);
    expect(tasks.tasks()[0]).toMatchObject({
      status: 'error',
      detail: 'It couldn’t continue just now, so nothing changed. Answer again from the bell.',
    });
  });

  it('a run the conversation answered first says so; any other error stands', async () => {
    const elsewhere = { ...ROW, resolution: { state: 'answered_elsewhere' as const, at: 'x' } };
    await sendAndSettle(outcome({ status: 'error' }), elsewhere);
    expect(tasks.tasks()[0].detail).toBe('It was already answered in the conversation.');

    tasks.dismiss(tasks.tasks()[0].id);
    inbox.set([]);
    await sendAndSettle(outcome({ status: 'timeout', error: 'Run timed out.' }));
    expect(tasks.tasks()[0]).toMatchObject({ status: 'error', detail: 'Run timed out.' });
  });

  it('a pause settled before the send shows the row settled and toasts why', async () => {
    closed.value = { kind: 'settled', state: 'expired', message: 'This expired before it was answered.' };
    await service.review(ROW);
    expect(notifications.settled).toHaveBeenCalledWith('n1', expect.objectContaining({ state: 'expired' }));
    expect(toast.info).toHaveBeenCalledWith('Expired', 'This expired before it was answered.');
    expect(tasks.tasks()).toEqual([]);
  });

  it('Open conversation goes to the paused run’s conversation; a cancel does nothing', async () => {
    closed.value = { kind: 'open' };
    await service.review(ROW);
    expect(router.navigate).toHaveBeenCalledWith(['/s', 'headless-7']);
    expect(sidenav.close).toHaveBeenCalled();

    vi.clearAllMocks();
    closed.value = undefined;
    await service.review(ROW);
    expect(router.navigate).not.toHaveBeenCalled();
    expect(notifications.settled).not.toHaveBeenCalled();
  });
});
