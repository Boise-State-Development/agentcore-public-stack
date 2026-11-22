import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { DeliveryStateService } from './delivery-state.service';
import { DeliveryView, HandoffStatus, HandoffView } from './handoff.models';
import { HandoffNotificationService } from './handoff-notification.service';
import { HandoffStateService } from './handoff-state.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { runProgress, showsInThread } from './workflow-run-progress';
import { WorkflowRunResultService } from './workflow-run-result.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { WorkflowRunView } from './workflow-run.models';

const API = 'https://api.test';
const COLLECT = `${API}/sessions/src/workflow-runs/run1/collect`;
const NAMES = ['Generalist', 'Colleague', 'Emotional outsider', 'Fiscal outsider'];

function row(i: number, status: HandoffStatus, target = status !== 'open'): HandoffView {
  return {
    handoffId: `h${i + 1}`,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: `a${i + 1}`,
    agentName: NAMES[i],
    request: 'r',
    sourceMessageIndex: 1,
    excerptTokens: 1,
    createdAt: '',
    updatedAt: '',
    runId: 'run1',
    stepId: `s${i + 1}`,
    ...(target ? { targetSessionId: `t${i + 1}` } : {}),
  };
}

function run(rows: (HandoffView | null)[], overrides: Partial<WorkflowRunView> = {}): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'r',
    steps: rows.map((r, i) => ({
      stepId: `s${i + 1}`,
      handoffId: `h${i + 1}`,
      agentId: `a${i + 1}`,
      agentName: NAMES[i],
      request: 'r',
      removed: false,
      handoff: r,
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 1,
    createdAt: '',
    updatedAt: '',
    ...overrides,
  };
}

function delivery(): DeliveryView {
  return {
    deliveryId: 'd1',
    fromSessionId: 't1',
    fromTitle: 'Review',
    origin: 'user',
    text: '',
    artifacts: [],
    createdAt: '2026-11-20T00:00:00Z',
    runId: 'run1',
    parts: [],
  };
}

describe('run progress', () => {
  const signals = (pending: string[] = [], loading: string[] = []) => ({
    isPending: (id: string) => pending.includes(id),
    isLoading: (id: string) => loading.includes(id),
  });

  it('reads each kept step from its live row first, then the replayed one', () => {
    const replayed = run([row(0, 'running'), row(1, 'open'), row(2, 'open'), null]);
    const progress = runProgress(replayed, [row(0, 'done')], signals(['h2']));
    expect(progress.lines.map((l) => l.state)).toEqual(['done', 'queued', 'not_started', 'stopped']);
    expect([progress.total, progress.done, progress.needsInput, progress.settled, progress.active]).toEqual([4, 1, 0, false, true]);
  });

  it('a step streaming in this tab is running, whatever its row says', () => {
    expect(runProgress(run([row(0, 'done')]), [], signals([], ['t1'])).lines[0].state).toBe('running');
  });

  it('a stopped run’s unstarted steps are stopped, so it settles', () => {
    const progress = runProgress(run([row(0, 'done'), row(1, 'open')], { status: 'stopped' }), [], signals());
    expect(progress.lines.map((l) => l.state)).toEqual(['done', 'stopped']);
    expect(progress.settled).toBe(true);
  });

  it('a removed step has no line, and only an approved or stopped run shows in the thread', () => {
    const r = run([row(0, 'done'), row(1, 'dismissed')]);
    r.steps[1].removed = true;
    expect(runProgress(r, [], signals()).total).toBe(1);
    expect(['open', 'approved', 'dismissed', 'stopped', 'abandoned'].map((status) => showsInThread({ ...r, status } as WorkflowRunView))).toEqual([
      false, true, false, true, false,
    ]);
  });
});

describe('WorkflowRunResultService', () => {
  let service: WorkflowRunResultService;
  let runs: WorkflowRunStateService;
  let handoffs: HandoffStateService;
  let deliveries: DeliveryStateService;
  let http: HttpTestingController;
  const toast = { success: vi.fn(), info: vi.fn(), error: vi.fn() };
  const openSource = vi.fn();

  beforeEach(() => {
    Object.values(toast).forEach((fn) => fn.mockClear());
    openSource.mockClear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false } },
        { provide: WorkflowRunQueueService, useValue: { isPending: () => false, isRunning: () => false } },
        { provide: HandoffNotificationService, useValue: { openSource } },
        { provide: ToastService, useValue: toast },
      ],
    });
    service = TestBed.inject(WorkflowRunResultService);
    runs = TestBed.inject(WorkflowRunStateService);
    handoffs = TestBed.inject(HandoffStateService);
    deliveries = TestBed.inject(DeliveryStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('collect puts the run’s one card on the source’s composer and records the result', async () => {
    runs.upsertRun(run([row(0, 'done'), row(1, 'running')]));
    const collected = service.collect({ runId: 'run1', sessionId: 'src' });
    http.expectOne(COLLECT).flush({ deliveryId: 'd1', created: true, status: 'pending', delivery: delivery() });
    await collected;

    expect(deliveries.cardsFor('src').map((c) => c.deliveryId)).toEqual(['d1']);
    expect(runs.runsFor('src')[0].resultDeliveryId).toBe('d1');
    expect(service.isOnComposer(runs.runsFor('src')[0])).toBe(true);
    expect(toast.success).toHaveBeenCalledWith('Results added to your next message');
  });

  it('a second collect reuses the card already made, and one already sent is not put back', async () => {
    runs.upsertRun(run([row(0, 'done')]));
    const again = service.collect({ runId: 'run1', sessionId: 'src' });
    http.expectOne(COLLECT).flush({ deliveryId: 'd1', created: false, status: 'pending', delivery: delivery() });
    await again;
    expect(deliveries.cardsFor('src')).toHaveLength(1);

    deliveries.remove('src', 'd1');
    const sent = service.collect({ runId: 'run1', sessionId: 'src' });
    http.expectOne(COLLECT).flush({ deliveryId: 'd1', created: false, status: 'attached', delivery: delivery() });
    await sent;
    expect(deliveries.cardsFor('src')).toEqual([]);
    expect(toast.info).toHaveBeenCalledWith('These results were already sent');
  });

  it('says why when nothing has finished yet', async () => {
    const collected = service.collect({ runId: 'run1', sessionId: 'src' });
    http.expectOne(COLLECT).flush({ detail: 'x' }, { status: 422, statusText: 'Unprocessable' });
    await collected;
    expect(toast.error).toHaveBeenCalledWith('The results couldn’t be brought back', 'No step has a result to bring back yet.');
  });

  it('a watched run that settles says so once and collects, quietly', async () => {
    runs.upsertRun(run([row(0, 'done'), row(1, 'running'), row(2, 'running')]));
    service.watch({ runId: 'run1', sessionId: 'src' });
    TestBed.tick();
    http.expectNone(COLLECT);

    handoffs.upsertRow(row(1, 'done'));
    handoffs.upsertRow(row(2, 'stopped'));
    TestBed.tick();

    expect(toast.success).toHaveBeenCalledWith('2 Agents finished', undefined, expect.objectContaining({ action: expect.anything() }));
    http.expectOne(COLLECT).flush({ deliveryId: 'd1', created: true, status: 'pending', delivery: delivery() });
    await vi.waitFor(() => expect(deliveries.cardsFor('src')).toHaveLength(1));
    expect(toast.success).toHaveBeenCalledTimes(1);

    // Open goes to the source, at the run's row.
    toast.success.mock.calls[0][2].action.handler();
    expect(openSource).toHaveBeenCalledWith({ handoffId: 'h1', sourceSessionId: 'src' });

    // Settled once: nothing more on the next change.
    handoffs.upsertRow(row(2, 'stopped'));
    TestBed.tick();
    expect(toast.success).toHaveBeenCalledTimes(1);
  });

  it('a stopped run is not collected for the user: the row offers Bring back results', () => {
    runs.upsertRun(run([row(0, 'done'), row(1, 'running')]));
    service.watch({ runId: 'run1', sessionId: 'src' });
    TestBed.tick();
    runs.upsertRun(run([row(0, 'done'), row(1, 'stopped')], { status: 'stopped' }));
    TestBed.tick();
    http.expectNone(COLLECT);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('a run nobody watched in this tab is never collected on its own', () => {
    runs.upsertRun(run([row(0, 'done'), row(1, 'done')]));
    TestBed.tick();
    http.expectNone(COLLECT);
  });

  it('counts prompt steps: a run settles once they do, says "steps", and collects (PR-D.6c)', async () => {
    const prompt = (i: number, status: HandoffStatus): HandoffView => ({ ...row(i, status, false), kind: 'prompt', agentId: '', agentName: 'Haiku 5.5' });
    const view = run([row(0, 'done'), prompt(1, 'running')]);
    view.steps[1] = { ...view.steps[1], kind: 'prompt', agentId: '', agentName: 'Haiku 5.5' };
    runs.upsertRun(view);
    service.watch({ runId: 'run1', sessionId: 'src' });
    TestBed.tick();
    http.expectNone(COLLECT);

    handoffs.upsertRow(prompt(1, 'done'));
    TestBed.tick();
    expect(toast.success).toHaveBeenCalledWith('2 steps finished', undefined, expect.anything());
    http.expectOne(COLLECT).flush({ deliveryId: 'd1', created: true, status: 'pending', delivery: delivery() });
    await vi.waitFor(() => expect(deliveries.cardsFor('src')).toHaveLength(1));
  });
});
