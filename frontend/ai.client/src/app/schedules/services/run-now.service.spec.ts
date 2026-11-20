import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { RUN_LOST_AFTER_MS, RUN_POLL_INTERVAL_MS, RunNowService } from './run-now.service';
import { RunApiService } from './run-api.service';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import { SessionService as SessionListService } from '../../session/services/session/session.service';
import { SessionMetadata } from '../../session/services/models/session-metadata.model';
import { RunStartedResponse, RunStatusResponse } from '../models/schedule.model';

const EMPTY_SESSION: SessionMetadata = {
  sessionId: '',
  userId: '',
  title: '',
  status: 'active',
  createdAt: '',
  lastMessageAt: '',
  messageCount: 0,
};

function started(overrides: Partial<RunStartedResponse> = {}): RunStartedResponse {
  return { runId: 'run-1', sessionId: 'sess-1', status: 'started', ...overrides };
}

function runStatus(overrides: Partial<RunStatusResponse> = {}): RunStatusResponse {
  return {
    runId: 'run-1',
    sessionId: 'sess-1',
    status: 'completed',
    title: null,
    error: null,
    startedAt: '2026-11-19T00:00:00Z',
    finishedAt: '2026-11-19T00:01:10Z',
    ...overrides,
  };
}

describe('RunNowService', () => {
  let service: RunNowService;
  let tasks: BackgroundTaskService;
  const mockRunApi = { runNow: vi.fn(), getRun: vi.fn() };
  const mockSessions = { refreshSessions: vi.fn(), currentSession: signal<SessionMetadata>(EMPTY_SESSION) };
  const mockRouter = { navigate: vi.fn() };

  /** Advance past `polls` poll ticks. */
  function poll(polls = 1): void {
    vi.advanceTimersByTime(RUN_POLL_INTERVAL_MS * polls);
  }

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    mockSessions.currentSession.set(EMPTY_SESSION);
    mockRunApi.runNow.mockReturnValue(of(started()));
    mockRunApi.getRun.mockReturnValue(of(runStatus()));
    TestBed.configureTestingModule({
      providers: [
        RunNowService,
        BackgroundTaskService,
        { provide: RunApiService, useValue: mockRunApi },
        { provide: SessionListService, useValue: mockSessions },
        { provide: Router, useValue: mockRouter },
      ],
    });
    service = TestBed.inject(RunNowService);
    tasks = TestBed.inject(BackgroundTaskService);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('registers a processing task immediately and fires the request', () => {
    service.run({ prompt: 'Go', title: 'My run' });

    expect(mockRunApi.runNow).toHaveBeenCalledWith({ prompt: 'Go', title: 'My run' });
    const [task] = tasks.tasks();
    expect(task.title).toContain('My run');
    expect(task.status).toBe('processing');
  });

  it('polls the started run and stays processing while it runs', () => {
    mockRunApi.getRun.mockReturnValue(of(runStatus({ status: 'started', finishedAt: null })));
    service.run({ prompt: 'Go' });

    poll(3);

    expect(mockRunApi.getRun).toHaveBeenCalledTimes(3);
    expect(mockRunApi.getRun).toHaveBeenCalledWith('run-1');
    expect(tasks.tasks()[0].status).toBe('processing');
    expect(service.running()).toBe(true);
  });

  it('completes the task with a session-detail route once the run finishes', () => {
    mockRunApi.runNow.mockReturnValue(of(started({ sessionId: 'sess-9' })));
    mockRunApi.getRun
      .mockReturnValueOnce(of(runStatus({ status: 'started', sessionId: 'sess-9' })))
      .mockReturnValue(of(runStatus({ sessionId: 'sess-9' })));
    service.run({ prompt: 'Go' });

    poll();
    expect(tasks.tasks()[0].status).toBe('processing');
    poll();

    const [task] = tasks.tasks();
    expect(task.status).toBe('completed');
    expect(task.route).toEqual(['/s', 'sess-9']);
    expect(mockSessions.refreshSessions).toHaveBeenCalledTimes(1);
  });

  it('stops polling once the run has finished', () => {
    service.run({ prompt: 'Go' });

    poll(5);

    expect(mockRunApi.getRun).toHaveBeenCalledTimes(1);
  });

  it('onView optimistically seeds the header title before navigating', () => {
    mockRunApi.runNow.mockReturnValue(of(started({ sessionId: 'sess-9' })));
    mockRunApi.getRun.mockReturnValue(of(runStatus({ sessionId: 'sess-9', title: 'My Briefing' })));
    service.run({ prompt: 'Go', title: 'My Briefing' });
    poll();

    const [task] = tasks.tasks();
    task.onView?.();

    // Header title reflects the run's session immediately (not the previous one).
    expect(mockSessions.currentSession().sessionId).toBe('sess-9');
    expect(mockSessions.currentSession().title).toBe('My Briefing');
    expect(mockRouter.navigate).toHaveBeenCalledWith(['/s', 'sess-9']);
  });

  it('fails the task but keeps a route and refreshes the list when a run errors', () => {
    mockRunApi.getRun.mockReturnValue(of(runStatus({ status: 'error', error: 'boom' })));
    service.run({ prompt: 'Go' });
    poll();

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toBe('boom');
    expect(task.route).toEqual(['/s', 'sess-1']);
    expect(mockSessions.refreshSessions).toHaveBeenCalled();
  });

  it('reports a timeout with no error text by its status', () => {
    mockRunApi.getRun.mockReturnValue(of(runStatus({ status: 'timeout' })));
    service.run({ prompt: 'Go' });
    poll();

    expect(tasks.tasks()[0].detail).toBe('Run timeout.');
  });

  it('marks oauth_required as an error needing account connection', () => {
    mockRunApi.getRun.mockReturnValue(of(runStatus({ status: 'oauth_required' })));
    service.run({ prompt: 'Go' });
    poll();

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toContain('connect an account');
  });

  it('keeps polling through a failed poll', () => {
    mockRunApi.getRun
      .mockReturnValueOnce(throwError(() => new Error('504')))
      .mockReturnValue(of(runStatus()));
    service.run({ prompt: 'Go' });

    poll();
    expect(tasks.tasks()[0].status).toBe('processing');
    poll();

    expect(tasks.tasks()[0].status).toBe('completed');
  });

  it('gives up on a run still started after the lost threshold', () => {
    mockRunApi.getRun.mockReturnValue(of(runStatus({ status: 'started', finishedAt: null })));
    service.run({ prompt: 'Go' });

    vi.advanceTimersByTime(RUN_LOST_AFTER_MS - RUN_POLL_INTERVAL_MS);
    expect(tasks.tasks()[0].status).toBe('processing');
    poll();

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toContain('Check your conversations');
    expect(task.route).toEqual(['/s', 'sess-1']);
    expect(mockSessions.refreshSessions).toHaveBeenCalled();
    expect(service.running()).toBe(false);
  });

  it('also gives up when every poll fails until the lost threshold', () => {
    mockRunApi.getRun.mockReturnValue(throwError(() => new Error('network down')));
    service.run({ prompt: 'Go' });

    vi.advanceTimersByTime(RUN_LOST_AFTER_MS);

    expect(tasks.tasks()[0].status).toBe('error');
    expect(service.running()).toBe(false);
  });

  it('fails the task on a start error without polling', () => {
    mockRunApi.runNow.mockReturnValue(throwError(() => new Error('network down')));
    service.run({ prompt: 'Go' });
    poll();

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toBe('network down');
    expect(task.route).toBeNull();
    expect(mockRunApi.getRun).not.toHaveBeenCalled();
    expect(mockSessions.refreshSessions).not.toHaveBeenCalled();
    expect(service.running()).toBe(false);
  });

  it('is running from the start until the run ends', () => {
    expect(service.running()).toBe(false);

    service.run({ prompt: 'Go' });
    expect(service.running()).toBe(true);

    poll();
    expect(service.running()).toBe(false);
  });
});
