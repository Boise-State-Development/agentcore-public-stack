import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { ChatStateService } from '../chat/chat-state.service';
import { AgentHandoffService, HandoffLaunch } from './agent-handoff.service';
import { HandoffView } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';
import { REFRESH_FIRST_MS, REFRESH_MAX_MS, WorkflowRunRefreshService } from './workflow-run-refresh.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { RunEnvelope, WorkflowRunView } from './workflow-run.models';

/**
 * A run's step rows re-read while a step runs on the server and not in this tab (PR-D.5a's
 * follow-up): after a reload, a reader queued behind a step that finished on the server starts,
 * and a stale "Working on" line catches up.
 */

const API = 'https://api.test';
const RUN_URL = `${API}/sessions/src/workflow-runs/run1`;

function row(handoffId: string, status: HandoffView['status'], updatedAt: string, targetSessionId?: string): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: `a-${handoffId}`,
    agentName: `Agent ${handoffId}`,
    request: 'review this',
    sourceMessageIndex: 0,
    excerptTokens: 0,
    createdAt: '2026-11-21T06:00:00Z',
    updatedAt,
    ...(targetSessionId ? { targetSessionId } : {}),
    runId: 'run1',
  };
}

function run(rows: HandoffView[], status: WorkflowRunView['status'] = 'approved'): WorkflowRunView {
  const names = ['generalist', 'colleague', 'summary'];
  return {
    runId: 'run1',
    sessionId: 'src',
    status,
    request: 'review this',
    steps: rows.map((handoff, i) => ({
      stepId: `s${i + 1}`,
      handoffId: handoff.handoffId,
      agentId: handoff.agentId,
      agentName: handoff.agentName,
      request: 'review this',
      removed: false,
      stepName: names[i],
      ...(i === 2 ? { reads: ['generalist', 'colleague'] } : {}),
      handoff,
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 0,
    createdAt: '2026-11-21T06:00:00Z',
    updatedAt: '2026-11-21T06:00:00Z',
    card: { text: 'card', removed: false },
  };
}

const ENVELOPE: RunEnvelope = {
  runId: 'run1',
  sourceSessionId: 'src',
  sourceTitle: 'Proposal',
  sourceMessageIndex: 0,
  excerptTokens: 0,
  fileIds: [],
  filesUnticked: 0,
  concurrency: 4,
};

/** As a reload leaves it: two reviewers still "running" here, the reader not started. */
const STALE = [
  row('h1', 'running', '2026-11-21T06:00:05Z', 't1'),
  row('h2', 'running', '2026-11-21T06:00:05Z', 't2'),
  row('h3', 'open', '2026-11-21T06:00:00Z'),
];
/** What the server says a minute later: both reviewers finished. */
const FINISHED = [
  row('h1', 'done', '2026-11-21T06:01:45Z', 't1'),
  row('h2', 'done', '2026-11-21T06:02:03Z', 't2'),
  row('h3', 'open', '2026-11-21T06:00:00Z'),
];

describe('WorkflowRunRefreshService', () => {
  let http: HttpTestingController;
  let runs: WorkflowRunStateService;
  let handoffs: HandoffStateService;
  let queue: WorkflowRunQueueService;
  let sends: HandoffLaunch[];
  const viewed = signal<string | null>('src');
  const loading = signal<ReadonlySet<string>>(new Set());

  beforeEach(() => {
    vi.useFakeTimers();
    sends = [];
    viewed.set('src');
    loading.set(new Set());
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        {
          provide: ChatStateService,
          useValue: { viewedSessionId: viewed, isSessionLoading: (id: string) => loading().has(id) },
        },
        {
          provide: AgentHandoffService,
          useValue: {
            startRunStep: (step: HandoffLaunch) => {
              sends.push(step);
              return new Promise<void>(() => undefined);
            },
            prepareRunStep: (_s: string, _r: string, stepId: string) =>
              Promise.resolve({ stepId, message: 'blocks\n\nsummarize', readsBlock: 'blocks', parts: [] }),
          },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    runs = TestBed.inject(WorkflowRunStateService);
    handoffs = TestBed.inject(HandoffStateService);
    queue = TestBed.inject(WorkflowRunQueueService);
  });

  afterEach(() => {
    http.verify();
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  /** The page as a reload leaves it: the replayed run and its rows. */
  function reloaded(rows = STALE): void {
    runs.hydrate('src', [run(rows)]);
    handoffs.hydrate('src', rows, null);
  }

  async function advance(ms: number): Promise<void> {
    await vi.advanceTimersByTimeAsync(ms);
    TestBed.tick();
  }

  it('starts a reader queued after a reload once its reads finish on the server', async () => {
    reloaded();
    TestBed.inject(WorkflowRunRefreshService);
    // Start on the row: the reader waits in the queue on two rows this tab can't see finish.
    queue.enqueue(ENVELOPE, [{ stepId: 's3', handoffId: 'h3', agentId: 'a-h3', agentName: 'Summary', request: 'summarize', reads: ['h1', 'h2'] }]);
    TestBed.tick();
    expect(queue.isPending('run1', 'h3')).toBe(true);

    await advance(REFRESH_FIRST_MS);
    http.expectOne(RUN_URL).flush(run(FINISHED));
    await advance(0);

    expect(handoffs.rowsFor('src').map((r) => r.status)).toEqual(['done', 'done', 'open']);
    await vi.waitFor(() => expect(sends.map((s) => s.handoffId)).toEqual(['h3']));
    expect(sends[0].message).toBe('blocks\n\nsummarize');
  });

  it('reads one run once a tick, backs off while nothing moves, and stops once it settles', async () => {
    reloaded();
    TestBed.inject(WorkflowRunRefreshService);
    TestBed.tick();

    await advance(REFRESH_FIRST_MS);
    http.expectOne(RUN_URL).flush(run(STALE));
    await advance(REFRESH_FIRST_MS);
    http.expectNone(RUN_URL);
    await advance(REFRESH_FIRST_MS);
    http.expectOne(RUN_URL).flush(run(STALE));
    await advance(REFRESH_MAX_MS);
    http.expectOne(RUN_URL).flush(run(FINISHED));
    await advance(0);

    // Settled: the line caught up, and nothing reads again.
    expect(handoffs.rowsFor('src').filter((r) => r.status === 'running')).toEqual([]);
    await advance(REFRESH_MAX_MS * 4);
    http.expectNone(RUN_URL);
  });

  it('reads nothing while the page is hidden, and reads again once it is back', async () => {
    reloaded();
    TestBed.inject(WorkflowRunRefreshService);
    TestBed.tick();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));

    await advance(REFRESH_MAX_MS * 3);
    http.expectNone(RUN_URL);

    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await advance(REFRESH_FIRST_MS);
    http.expectOne(RUN_URL).flush(run(FINISHED));
    visibility.mockRestore();
  });

  it('never reads a run whose steps all stream in this tab, or one not on screen or queued', async () => {
    reloaded();
    loading.set(new Set(['t1', 't2']));
    TestBed.inject(WorkflowRunRefreshService);
    TestBed.tick();
    await advance(REFRESH_MAX_MS);
    http.expectNone(RUN_URL);

    loading.set(new Set());
    viewed.set('another-conversation');
    TestBed.tick();
    await advance(REFRESH_MAX_MS);
    http.expectNone(RUN_URL);
  });

  it('never overwrites a row this tab streams, nor with an older copy', async () => {
    reloaded([row('h1', 'running', '2026-11-21T06:00:05Z', 't1'), row('h2', 'running', '2026-11-21T06:03:00Z', 't2'), row('h3', 'open', '2026-11-21T06:00:00Z')]);
    // A reviewer resumed in this tab streams again; the other is not streaming here.
    loading.set(new Set(['t1']));
    TestBed.inject(WorkflowRunRefreshService);
    TestBed.tick();

    await advance(REFRESH_FIRST_MS);
    http.expectOne(RUN_URL).flush(
      run([row('h1', 'done', '2026-11-21T06:09:00Z', 't1'), row('h2', 'done', '2026-11-21T06:02:00Z', 't2'), row('h3', 'open', '2026-11-21T06:00:00Z')]),
    );
    await advance(0);

    // h1 is live here, and the server's h2 is older than the one this tab holds.
    expect(handoffs.rowsFor('src').map((r) => r.status)).toEqual(['running', 'running', 'open']);
  });

  it('follows a prompt step running on the server after a reload, which has no conversation to stream (PR-D.6c)', async () => {
    const prompt = (status: HandoffView['status'], updatedAt: string, outputText?: string): HandoffView => ({
      ...row('h1', status, updatedAt),
      kind: 'prompt',
      agentId: '',
      agentName: 'Haiku 5.5',
      ...(outputText ? { outputText } : {}),
    });
    const stale = run([prompt('running', '2026-11-21T06:00:05Z'), row('h2', 'done', '2026-11-21T06:00:05Z', 't2')]);
    stale.steps[0] = { ...stale.steps[0], kind: 'prompt', agentId: '', reads: ['request', 'files'] };
    runs.hydrate('src', [stale]);
    handoffs.hydrate('src', [stale.steps[0].handoff!, stale.steps[1].handoff!], null);
    const refresh = TestBed.inject(WorkflowRunRefreshService);
    TestBed.tick();
    expect(refresh.stale().has('run1')).toBe(true);

    await advance(REFRESH_FIRST_MS);
    const done = run([prompt('done', '2026-11-21T06:01:00Z', 'The aims are clear.'), row('h2', 'done', '2026-11-21T06:00:05Z', 't2')]);
    done.steps[0] = { ...done.steps[0], kind: 'prompt', agentId: '', reads: ['request', 'files'] };
    http.expectOne(RUN_URL).flush(done);
    await advance(0);

    expect(handoffs.rowsFor('src')[0]).toMatchObject({ status: 'done', outputText: 'The aims are clear.' });
    expect(refresh.stale().has('run1')).toBe(false);
  });
});
