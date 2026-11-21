import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../services/config.service';
import { WorkflowStartService } from '../services/workflows/workflow-start.service';
import { UserConnectorsService } from '../settings/connectors/services/user-connectors.service';
import { ChatStateService } from '../session/services/chat/chat-state.service';
import { ToolInsightService } from '../session/services/chat/tool-insight.service';
import { HandoffStateService } from '../session/services/handoff/handoff-state.service';
import { HandoffView } from '../session/services/handoff/handoff.models';
import { RunStepLine } from '../session/services/handoff/workflow-run-progress';
import { WorkflowRunQueueService } from '../session/services/handoff/workflow-run-queue.service';
import { WorkflowRunStateService } from '../session/services/handoff/workflow-run-state.service';
import { WorkflowRunView } from '../session/services/handoff/workflow-run.models';
import { WorkflowTestRunService } from './workflow-test-run.service';

const API = 'https://api.test';
const REF = { skillId: 'SKL-1', name: 'proposal-review' };
const STORAGE_KEY = 'aps.workflowTestRuns';

function handoff(status: HandoffView['status'], extra: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId: 'h1',
    sessionId: 'test-conv',
    kind: 'mention',
    status,
    agentId: 'a1',
    agentName: 'Generalist',
    request: 'review this',
    sourceMessageIndex: 0,
    excerptTokens: 0,
    createdAt: '',
    updatedAt: '',
    targetSessionId: 't1',
    runId: 'RUN-1',
    ...extra,
  };
}

function run(status: WorkflowRunView['status'], row: HandoffView = handoff('running')): WorkflowRunView {
  return {
    runId: 'RUN-1',
    sessionId: 'test-conv',
    status,
    request: 'review this',
    steps: [{ stepId: 's1', handoffId: 'h1', agentId: 'a1', agentName: 'Generalist', request: 'review this', removed: false, handoff: row }],
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 0,
    createdAt: '',
    updatedAt: '',
  };
}

describe('WorkflowTestRunService', () => {
  let http: HttpTestingController;
  const start = vi.fn();

  function create(): WorkflowTestRunService {
    return TestBed.inject(WorkflowTestRunService);
  }

  beforeEach(() => {
    sessionStorage.clear();
    start.mockReset().mockResolvedValue('test-conv');
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: WorkflowStartService, useValue: { start } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false } },
        { provide: WorkflowRunQueueService, useValue: { isPending: () => false } },
        { provide: ToolInsightService, useValue: { latestSummary: () => null, status: () => null } },
        { provide: UserConnectorsService, useValue: { displayNameFor: (id: string) => id } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    sessionStorage.clear();
  });

  it('opens the sheet in a "Test: …" conversation without navigating, and remembers it', async () => {
    const service = create();

    expect(await service.begin(REF)).toBe(true);

    expect(start).toHaveBeenCalledWith(REF, { navigate: false, titlePrefix: 'Test: ' });
    expect(service.recordFor(REF)).toEqual({ sessionId: 'test-conv' });
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY)!)).toEqual({ 'SKL-1/proposal-review': { sessionId: 'test-conv' } });
  });

  it('records nothing when the start failed', async () => {
    start.mockResolvedValue(null);
    const service = create();

    expect(await service.begin(REF)).toBe(false);
    expect(service.recordFor(REF)).toBeUndefined();
  });

  it('finds the record again after a reload, and forgets it on request', () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ 'SKL-1/proposal-review': { sessionId: 'test-conv', runId: 'RUN-1' }, bad: { nope: 1 } }));
    const service = create();

    expect(service.recordFor(REF)).toEqual({ sessionId: 'test-conv', runId: 'RUN-1' });
    expect(service.recordFor({ skillId: 'bad', name: '' })).toBeUndefined();
    service.forget(REF);
    expect(service.recordFor(REF)).toBeUndefined();
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY)!)).toEqual({});
  });

  it('loads a started run by id into run state, where the run’s row reads it', async () => {
    const service = create();
    await service.begin(REF);
    service.setRunId(REF, 'RUN-1');

    const loading = service.load({ sessionId: 'test-conv', runId: 'RUN-1' });
    http.expectOne(`${API}/sessions/test-conv/workflow-runs/RUN-1`).flush(run('approved'));

    expect(await loading).toBe('loaded');
    expect(service.runFor(REF)?.runId).toBe('RUN-1');
    expect(TestBed.inject(WorkflowRunStateService).runsFor('test-conv')).toHaveLength(1);
  });

  it('calls a run that was never approved, or is gone, gone', async () => {
    const service = create();

    const open = service.load({ sessionId: 'test-conv', runId: 'RUN-1' });
    http.expectOne(`${API}/sessions/test-conv/workflow-runs/RUN-1`).flush(run('dismissed'));
    expect(await open).toBe('gone');

    const missing = service.load({ sessionId: 'test-conv', runId: 'RUN-1' });
    http.expectOne(`${API}/sessions/test-conv/workflow-runs/RUN-1`).flush({}, { status: 404, statusText: 'Not Found' });
    expect(await missing).toBe('gone');

    const failed = service.load({ sessionId: 'test-conv', runId: 'RUN-1' });
    http.expectOne(`${API}/sessions/test-conv/workflow-runs/RUN-1`).flush({}, { status: 500, statusText: 'x' });
    expect(await failed).toBe('failed');
  });

  it('reads each step from the live handoff row the run’s row reads, and words it the same', () => {
    const service = create();
    const approved = run('approved', handoff('running'));
    TestBed.inject(HandoffStateService).upsertRow(
      handoff('needs_input', { waitingOn: { kind: 'tool_approval', name: 'budget_check' } }),
    );

    const progress = service.progress(approved);

    expect(progress.lines.map((line) => line.state)).toEqual(['needs_input']);
    expect(service.phrase(progress.lines[0])).toBe('Waiting for your approval to run budget_check');
    const queued: RunStepLine = { ...progress.lines[0], state: 'queued', row: null };
    expect(service.phrase(queued)).toBe('Queued');
  });
});
