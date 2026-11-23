import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentHandoffService, HandoffLaunch } from './agent-handoff.service';
import { HandoffView } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { PrepareRunStepResponse, RunEnvelope, RunStepLaunch, WorkflowRunView } from './workflow-run.models';

const ENVELOPE: RunEnvelope = {
  runId: 'run1',
  sourceSessionId: 'src',
  sourceTitle: 'Proposal',
  sourceMessageIndex: 3,
  excerptTokens: 20,
  contextText: 'reviewed card',
  fileIds: ['f1'],
  filesUnticked: 0,
  concurrency: 4,
};

const STEPS: RunStepLaunch[] = Array.from({ length: 5 }, (_, i) => ({
  stepId: `s${i + 1}`,
  handoffId: `h${i + 1}`,
  agentId: `a${i + 1}`,
  agentName: `Agent ${i + 1}`,
  request: 'review this',
}));

/** A `PageTransitionEvent` as the browser fires it; happy-dom has no constructor for one. */
function pageEvent(type: 'pagehide' | 'pageshow', persisted: boolean): Event {
  return Object.assign(new Event(type), { persisted });
}

describe('WorkflowRunQueueService while the page unloads', () => {
  let queue: WorkflowRunQueueService;
  let sends: { step: HandoffLaunch; resolve: () => void; reject: (e: Error) => void }[];

  beforeEach(() => {
    sends = [];
    TestBed.configureTestingModule({
      providers: [
        HandoffStateService,
        {
          provide: AgentHandoffService,
          useValue: {
            startRunStep: (step: HandoffLaunch) =>
              new Promise<void>((resolve, reject) => sends.push({ step, resolve, reject })),
            prepareRunStep: () => Promise.reject(new Error('a run of mentions never prepares a step')),
          },
        },
      ],
    });
    queue = TestBed.inject(WorkflowRunQueueService);
  });

  afterEach(() => TestBed.resetTestingModule());

  const started = (): string[] => sends.map((send) => send.step.handoffId);

  it('starts four, and the fifth when one settles', async () => {
    queue.enqueue(ENVELOPE, STEPS);
    expect(started()).toEqual(['h1', 'h2', 'h3', 'h4']);
    sends[0].resolve();
    await vi.waitFor(() => expect(started()).toContain('h5'));
  });

  it('starts nothing once the page is hiding, when the aborted streams settle their slots', async () => {
    queue.enqueue(ENVELOPE, STEPS);
    expect(started()).toHaveLength(4);

    window.dispatchEvent(pageEvent('pagehide', false));
    // Unloading aborts every running stream.
    for (const send of sends) send.reject(new Error('aborted'));
    await new Promise((resolve) => setTimeout(resolve));

    expect(started()).toEqual(['h1', 'h2', 'h3', 'h4']);
    // The queued step was never sent, so its row stays open and a reload offers it as "Not started".
    expect(queue.isPending('run1', 'h5')).toBe(true);
  });

  it('carries on when the page comes back from the back/forward cache', async () => {
    queue.enqueue(ENVELOPE, STEPS);
    window.dispatchEvent(pageEvent('pagehide', true));
    sends[0].resolve();
    await new Promise((resolve) => setTimeout(resolve));
    expect(started()).not.toContain('h5');

    window.dispatchEvent(pageEvent('pageshow', true));
    expect(started()).toContain('h5');
  });

  it('sends a step of a run of mentions with its request, as before stages', () => {
    queue.enqueue(ENVELOPE, STEPS);
    expect('message' in sends[0].step).toBe(false);
    expect(sends[0].step.request).toBe('review this');
  });

  it('ignores an ordinary pageshow', () => {
    window.dispatchEvent(pageEvent('pageshow', false));
    queue.enqueue(ENVELOPE, STEPS);
    expect(started()).toHaveLength(4);
  });
});

/** A step's handoff row as the tab holds it. */
function row(handoffId: string, status: HandoffView['status'], targetSessionId?: string): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: `agent-${handoffId}`,
    agentName: `Agent ${handoffId}`,
    request: 'review this',
    sourceMessageIndex: 3,
    excerptTokens: 20,
    createdAt: '2026-11-20T00:00:00Z',
    updatedAt: '2026-11-20T00:00:00Z',
    ...(targetSessionId ? { targetSessionId } : {}),
    runId: 'run1',
  };
}

/** Two reviewers and a summary that reads both (PR-D.5a). */
const STAGED: RunStepLaunch[] = [
  { stepId: 's1', handoffId: 'h1', agentId: 'a1', agentName: 'Generalist', request: 'review this' },
  { stepId: 's2', handoffId: 'h2', agentId: 'a2', agentName: 'Colleague', request: 'review this' },
  { stepId: 's3', handoffId: 'h3', agentId: 'a3', agentName: 'Summary', request: 'summarize', reads: ['h1', 'h2'] },
];

function stagedRun(rows: Record<string, HandoffView>): WorkflowRunView {
  const names = ['generalist', 'colleague', 'summary'];
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'review this',
    steps: STAGED.map((step, i) => ({
      stepId: step.stepId,
      handoffId: step.handoffId,
      agentId: step.agentId,
      agentName: step.agentName,
      request: step.request,
      removed: false,
      stepName: names[i],
      ...(i === 2 ? { reads: ['generalist', 'colleague'] } : {}),
      handoff: rows[step.handoffId] ?? row(step.handoffId, 'open'),
    })),
    fileIds: ['f1'],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 3,
    createdAt: '2026-11-20T00:00:00Z',
    updatedAt: '2026-11-20T00:00:00Z',
    card: { text: 'reviewed card', removed: false },
  };
}

describe('WorkflowRunQueueService with stages', () => {
  let queue: WorkflowRunQueueService;
  let handoffState: HandoffStateService;
  let runState: WorkflowRunStateService;
  let sends: { step: HandoffLaunch; resolve: () => void }[];
  let prepares: string[];
  let prepare: (stepId: string) => Promise<PrepareRunStepResponse>;

  beforeEach(() => {
    sends = [];
    prepares = [];
    prepare = (stepId) =>
      Promise.resolve({ stepId, message: '<handoff_context …>\n\nsummarize', readsBlock: '<handoff_context …>', parts: [] });
    TestBed.configureTestingModule({
      providers: [
        HandoffStateService,
        WorkflowRunStateService,
        {
          provide: AgentHandoffService,
          useValue: {
            startRunStep: (step: HandoffLaunch) => new Promise<void>((resolve) => sends.push({ step, resolve })),
            prepareRunStep: (_source: string, _run: string, stepId: string) => {
              prepares.push(stepId);
              return prepare(stepId);
            },
          },
        },
      ],
    });
    queue = TestBed.inject(WorkflowRunQueueService);
    handoffState = TestBed.inject(HandoffStateService);
    runState = TestBed.inject(WorkflowRunStateService);
  });

  afterEach(() => TestBed.resetTestingModule());

  const started = (): string[] => sends.map((send) => send.step.handoffId);

  it('starts a reader only once every step it reads has settled, with the prepared message', async () => {
    runState.upsertRun(stagedRun({}));
    queue.enqueue(ENVELOPE, STAGED);

    expect(started()).toEqual(['h1', 'h2']);
    expect(queue.isPending('run1', 'h3')).toBe(true);

    handoffState.upsertRow(row('h1', 'done', 't1'));
    sends[0].resolve();
    await new Promise((resolve) => setTimeout(resolve));
    expect(prepares).toEqual([]);

    // A step waiting on the user hasn't settled: its answer is still to come.
    handoffState.upsertRow(row('h2', 'needs_input', 't2'));
    await new Promise((resolve) => setTimeout(resolve));
    expect(prepares).toEqual([]);

    // A stopped step has settled, so the reader starts and its message names it.
    handoffState.upsertRow(row('h2', 'stopped', 't2'));
    await vi.waitFor(() => expect(started()).toContain('h3'));
    expect(prepares).toEqual(['s3']);
    const reader = sends.find((send) => send.step.handoffId === 'h3')!.step;
    expect(reader.message).toBe('<handoff_context …>\n\nsummarize');
    expect(reader.request).toBe('summarize');
  });

  it('retries a 409 while the server catches up, then sends', async () => {
    let calls = 0;
    prepare = (stepId) => {
      calls += 1;
      if (calls === 1) return Promise.reject(new HttpErrorResponse({ status: 409 }));
      return Promise.resolve({ stepId, message: 'blocks\n\nsummarize', readsBlock: 'blocks', parts: [] });
    };
    runState.upsertRun(stagedRun({ h1: row('h1', 'done', 't1'), h2: row('h2', 'done', 't2') }));
    handoffState.upsertRow(row('h1', 'done', 't1'));
    handoffState.upsertRow(row('h2', 'done', 't2'));

    queue.enqueue(ENVELOPE, [STAGED[2]]);
    expect(queue.isPending('run1', 'h3')).toBe(true);

    await vi.waitFor(() => expect(started()).toEqual(['h3']), { timeout: 2000 });
    expect(calls).toBe(2);
  });

  it('after a reload, starts the reviewers left and holds the reader until they settle', async () => {
    const run = stagedRun({ h1: row('h1', 'done', 't1') });
    runState.upsertRun(run);
    handoffState.upsertRow(row('h1', 'done', 't1'));

    const unstarted = queue.unstartedSteps(run);
    expect(unstarted.map((step) => [step.handoffId, step.reads])).toEqual([
      ['h2', []],
      ['h3', ['h1', 'h2']],
    ]);
    queue.enqueue(ENVELOPE, unstarted);
    expect(started()).toEqual(['h2']);

    handoffState.upsertRow(row('h2', 'done', 't2'));
    await vi.waitFor(() => expect(started()).toEqual(['h2', 'h3']));
  });

  it('lets go of a reader whose read step will never start here, so the row offers Start', async () => {
    runState.upsertRun(stagedRun({ h1: row('h1', 'done', 't1') }));
    handoffState.upsertRow(row('h1', 'done', 't1'));

    queue.enqueue(ENVELOPE, [STAGED[2]]);

    expect(started()).toEqual([]);
    expect(queue.isQueued('run1')).toBe(false);
  });

  it('a step that can’t be prepared frees its slot and is not sent', async () => {
    prepare = () => Promise.reject(new HttpErrorResponse({ status: 404 }));
    runState.upsertRun(stagedRun({ h1: row('h1', 'done', 't1'), h2: row('h2', 'stopped', 't2') }));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    queue.enqueue(ENVELOPE, [STAGED[2]]);

    await vi.waitFor(() => expect(queue.isQueued('run1')).toBe(false));
    expect(started()).toEqual([]);
  });
});
