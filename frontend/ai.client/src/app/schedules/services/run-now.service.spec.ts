import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { RunNowService } from './run-now.service';
import { RunApiService } from './run-api.service';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import { SessionService as SessionListService } from '../../session/services/session/session.service';
import { SessionMetadata } from '../../session/services/models/session-metadata.model';
import { RunNowResponse } from '../models/schedule.model';

const EMPTY_SESSION: SessionMetadata = {
  sessionId: '',
  userId: '',
  title: '',
  status: 'active',
  createdAt: '',
  lastMessageAt: '',
  messageCount: 0,
};

function response(overrides: Partial<RunNowResponse> = {}): RunNowResponse {
  return {
    runId: 'run-1',
    sessionId: 'sess-1',
    status: 'completed',
    finalMessage: 'done',
    stopReason: null,
    error: null,
    title: null,
    ...overrides,
  };
}

describe('RunNowService', () => {
  let service: RunNowService;
  let tasks: BackgroundTaskService;
  const mockRunApi = { runNow: vi.fn() };
  const mockSessions = { refreshSessions: vi.fn(), currentSession: signal<SessionMetadata>(EMPTY_SESSION) };
  const mockRouter = { navigate: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    mockSessions.currentSession.set(EMPTY_SESSION);
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

  it('registers a processing task immediately and fires the request', () => {
    mockRunApi.runNow.mockReturnValue(of(response()));
    service.run({ prompt: 'Go', title: 'My run' });

    expect(mockRunApi.runNow).toHaveBeenCalledWith({ prompt: 'Go', title: 'My run' });
    const [task] = tasks.tasks();
    expect(task.title).toContain('My run');
  });

  it('completes the task with a session-detail route on success and refreshes the sidebar list', () => {
    mockRunApi.runNow.mockReturnValue(of(response({ sessionId: 'sess-9' })));
    service.run({ prompt: 'Go' });

    const [task] = tasks.tasks();
    expect(task.status).toBe('completed');
    expect(task.route).toEqual(['/s', 'sess-9']);
    expect(mockSessions.refreshSessions).toHaveBeenCalled();
  });

  it('onView optimistically seeds the header title before navigating', () => {
    mockRunApi.runNow.mockReturnValue(of(response({ sessionId: 'sess-9', title: 'My Briefing' })));
    service.run({ prompt: 'Go', title: 'My Briefing' });

    const [task] = tasks.tasks();
    task.onView?.();

    // Header title reflects the run's session immediately (not the previous one).
    expect(mockSessions.currentSession().sessionId).toBe('sess-9');
    expect(mockSessions.currentSession().title).toBe('My Briefing');
    expect(mockRouter.navigate).toHaveBeenCalledWith(['/s', 'sess-9']);
  });

  it('fails the task but keeps a route when a run errors with a session', () => {
    mockRunApi.runNow.mockReturnValue(of(response({ status: 'error', error: 'boom', sessionId: 'sess-3' })));
    service.run({ prompt: 'Go' });

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toBe('boom');
    expect(task.route).toEqual(['/s', 'sess-3']);
  });

  it('marks oauth_required as an error needing account connection', () => {
    mockRunApi.runNow.mockReturnValue(of(response({ status: 'oauth_required' })));
    service.run({ prompt: 'Go' });

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toContain('connect an account');
  });

  it('fails the task on a transport error', () => {
    mockRunApi.runNow.mockReturnValue(throwError(() => new Error('network down')));
    service.run({ prompt: 'Go' });

    const [task] = tasks.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toBe('network down');
    expect(task.route).toBeNull();
    expect(mockSessions.refreshSessions).not.toHaveBeenCalled();
  });
});
