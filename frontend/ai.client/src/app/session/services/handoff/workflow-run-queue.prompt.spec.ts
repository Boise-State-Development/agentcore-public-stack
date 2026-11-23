import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastService } from '../../../services/toast/toast.service';
import { AgentHandoffService, HandoffLaunch } from './agent-handoff.service';
import { HandoffView } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';
import { PromptStepFrame, WorkflowPromptHandlers, WorkflowPromptService } from './workflow-prompt.service';
import { PROMPT_READS_ATTEMPTS, PROMPT_READS_RETRY_MS, WorkflowRunQueueService, launchOf } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { RunEnvelope, RunStepLaunch, WorkflowRunStepView, WorkflowRunView } from './workflow-run.models';
import { runProgress } from './workflow-run-progress';

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

function row(handoffId: string, status: HandoffView['status'], patch: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: 'prompt',
    status,
    agentId: '',
    agentName: 'Haiku 5.5',
    modelId: 'us.anthropic.claude-haiku-5-5',
    request: 'review this',
    sourceMessageIndex: 3,
    excerptTokens: 0,
    createdAt: '2026-11-21T00:00:00Z',
    updatedAt: '2026-11-21T00:00:00Z',
    runId: 'run1',
    ...patch,
  };
}

/** Five prompt lenses reading the request and the files, an Agent step, and a prompt summary. */
function run(rows: Record<string, HandoffView> = {}): WorkflowRunView {
  const lens = (i: number): WorkflowRunStepView => ({
    stepId: `s${i}`,
    handoffId: `h${i}`,
    kind: 'prompt',
    agentId: '',
    agentName: 'Haiku 5.5',
    modelId: 'us.anthropic.claude-haiku-5-5',
    request: `lens ${i}`,
    removed: false,
    stepName: `lens_${i}`,
    reads: ['request', 'files'],
    handoff: rows[`h${i}`] ?? row(`h${i}`, 'open'),
  });
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'review this',
    steps: [
      lens(1),
      lens(2),
      lens(3),
      {
        stepId: 'sa',
        handoffId: 'ha',
        agentId: 'agent-a',
        agentName: 'Auditor',
        request: 'audit',
        removed: false,
        stepName: 'auditor',
        handoff: rows['ha'] ?? { ...row('ha', 'open'), kind: 'mention', agentId: 'agent-a', agentName: 'Auditor' },
      },
      {
        stepId: 'ss',
        handoffId: 'hs',
        kind: 'prompt',
        agentId: '',
        agentName: 'Sonnet 5.5',
        modelId: 'us.anthropic.claude-sonnet-5-5',
        request: 'summarize',
        removed: false,
        stepName: 'summary',
        // Reads two lenses and no files: its inputs are kept first, as the run row writes them.
        reads: ['request', 'lens_1', 'lens_2'],
        handoff: rows['hs'] ?? row('hs', 'open', { agentName: 'Sonnet 5.5' }),
      },
    ],
    fileIds: ['f1'],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 3,
    createdAt: '2026-11-21T00:00:00Z',
    updatedAt: '2026-11-21T00:00:00Z',
    card: { text: 'reviewed card', removed: false },
  };
}

interface Call {
  runId: string;
  stepIds: readonly string[];
  handlers: WorkflowPromptHandlers;
  close: () => void;
}

describe('WorkflowRunQueueService with prompt steps (PR-D.6c)', () => {
  let queue: WorkflowRunQueueService;
  let handoffState: HandoffStateService;
  let runState: WorkflowRunStateService;
  let calls: Call[];
  let sends: HandoffLaunch[];
  let serverRows: Map<string, HandoffView>;
  let reads: string[];
  let runReads: string[];
  let serverRun: WorkflowRunView | null;
  let toast: { error: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    calls = [];
    sends = [];
    reads = [];
    runReads = [];
    serverRun = null;
    serverRows = new Map();
    toast = { error: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        HandoffStateService,
        WorkflowRunStateService,
        { provide: ToastService, useValue: toast },
        {
          provide: WorkflowPromptService,
          useValue: {
            run: (_source: string, runId: string, stepIds: readonly string[], handlers: WorkflowPromptHandlers) =>
              new Promise<void>((resolve) => calls.push({ runId, stepIds, handlers, close: resolve })),
          },
        },
        {
          provide: AgentHandoffService,
          useValue: {
            startRunStep: (step: HandoffLaunch) => {
              sends.push(step);
              return new Promise<void>(() => undefined);
            },
            prepareRunStep: (_s: string, _r: string, stepId: string) =>
              Promise.resolve({ stepId, message: 'blocks', readsBlock: 'blocks', parts: [] }),
            readRun: (_s: string, runId: string) => {
              runReads.push(runId);
              return Promise.resolve(serverRun);
            },
            readRow: (_s: string, handoffId: string) => {
              reads.push(handoffId);
              return Promise.resolve(serverRows.get(handoffId) ?? null);
            },
          },
        },
      ],
    });
    queue = TestBed.inject(WorkflowRunQueueService);
    handoffState = TestBed.inject(HandoffStateService);
    runState = TestBed.inject(WorkflowRunStateService);
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  const launches = (view: WorkflowRunView): RunStepLaunch[] => view.steps.map((step) => launchOf(view, step));
  const frame = (call: Call, f: PromptStepFrame) => call.handlers.onStep(f);
  const tick = () => new Promise((resolve) => setTimeout(resolve));

  it('sends the ready prompt steps together on one invocation, beside the Agent step’s slot', () => {
    const view = run();
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, launches(view));

    expect(calls).toHaveLength(1);
    expect(calls[0].stepIds).toEqual(['s1', 's2', 's3']);
    // A prompt step holds no Agent slot: the Auditor starts at once beside the three.
    expect(sends.map((s) => s.handoffId)).toEqual(['ha']);
    // The summary reads lens 1 and 2 by name; its `request` input is never a step it waits on.
    expect(queue.isPending('run1', 'hs')).toBe(true);
  });

  it('reads a prompt step in flight as running, and settles it from its frame and its row', async () => {
    const view = run();
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, launches(view));
    const signals = {
      isLoading: () => false,
      isPending: (id: string) => queue.isPending('run1', id),
      isRunning: (id: string) => queue.isRunning('run1', id),
    };
    expect(runProgress(view, handoffState.rowsFor('src'), signals).lines[0].state).toBe('running');

    serverRows.set('h1', row('h1', 'done', { outputText: 'The budget is thin.', updatedAt: '2026-11-21T00:01:00Z' }));
    frame(calls[0], { stepId: 's1', status: 'done' });
    expect(queue.isRunning('run1', 'h1')).toBe(false);
    // Shown at once from the frame, then the row read gives its answer.
    expect(handoffState.rowsFor('src').find((r) => r.handoffId === 'h1')?.status).toBe('done');
    await vi.waitFor(() => expect(handoffState.rowsFor('src').find((r) => r.handoffId === 'h1')?.outputText).toBe('The budget is thin.'));
    expect(runProgress(view, handoffState.rowsFor('src'), signals).lines[0].state).toBe('done');

    frame(calls[0], { stepId: 's2', status: 'stopped', stoppedReason: 'too_large' });
    expect(handoffState.rowsFor('src').find((r) => r.handoffId === 'h2')?.stoppedReason).toBe('too_large');
  });

  it('sends a later prompt step on the next invocation once its reads and the first invocation’s steps have settled', async () => {
    const view = run();
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, launches(view));

    frame(calls[0], { stepId: 's1', status: 'done' });
    frame(calls[0], { stepId: 's2', status: 'done' });
    await tick();
    // Its reads have settled, but one prompt invocation per run is in flight at a time.
    expect(calls).toHaveLength(1);

    frame(calls[0], { stepId: 's3', status: 'done' });
    calls[0].close();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].stepIds).toEqual(['ss']);
  });

  it('waits and sends a reads_pending step again, a bounded number of times, never in a loop', async () => {
    vi.useFakeTimers();
    const view = run({ h1: row('h1', 'done'), h2: row('h2', 'done') });
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, [launchOf(view, view.steps[4])]);
    expect(calls).toHaveLength(1);

    for (let attempt = 1; attempt < PROMPT_READS_ATTEMPTS; attempt++) {
      const call = calls[calls.length - 1];
      frame(call, { stepId: 'ss', status: 'skipped', reason: 'reads_pending' });
      call.close();
      await vi.advanceTimersByTimeAsync(0);
      // Held: not sent again before its wait is over.
      expect(calls).toHaveLength(attempt);
      await vi.advanceTimersByTimeAsync(PROMPT_READS_RETRY_MS * attempt);
      expect(calls).toHaveLength(attempt + 1);
    }
    const last = calls[calls.length - 1];
    frame(last, { stepId: 'ss', status: 'skipped', reason: 'reads_pending' });
    last.close();
    await vi.advanceTimersByTimeAsync(PROMPT_READS_RETRY_MS * PROMPT_READS_ATTEMPTS * 2);
    expect(calls).toHaveLength(PROMPT_READS_ATTEMPTS);
    // Left for the row's Start: the queue let it go.
    expect(queue.isQueued('run1')).toBe(false);
  });

  it('re-reads a not_open step instead of sending it again: another tab or call has it', async () => {
    const view = run();
    runState.upsertRun(view);
    serverRows.set('h1', row('h1', 'running', { updatedAt: '2026-11-21T00:00:05Z' }));
    queue.enqueue(ENVELOPE, launches(view));

    frame(calls[0], { stepId: 's1', status: 'skipped', reason: 'not_open' });
    await vi.waitFor(() => expect(handoffState.rowsFor('src').find((r) => r.handoffId === 'h1')?.status).toBe('running'));
    expect(queue.isRunning('run1', 'h1')).toBe(false);
    expect(calls).toHaveLength(1);
  });

  it('re-reads the rows of steps a closed stream gave no frame', async () => {
    const view = run();
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, launches(view));
    frame(calls[0], { stepId: 's1', status: 'done' });
    calls[0].close();
    await vi.waitFor(() => expect(reads).toEqual(expect.arrayContaining(['h1', 'h2', 'h3'])));
    expect(queue.isRunning('run1', 'h2')).toBe(false);
  });

  it('says why when the whole request is refused, and leaves the steps not started', async () => {
    const view = run();
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, launches(view));
    calls[0].handlers.onRefused('flag_off');
    calls[0].close();
    await vi.waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(reads).toEqual([]);
    expect(queue.isRunning('run1', 'h1')).toBe(false);
  });

  it('keeps reading frames after Stop run closed the queue: the Runtime stops the steps', () => {
    const view = run();
    runState.upsertRun(view);
    queue.enqueue(ENVELOPE, launches(view));
    queue.stop('run1');
    frame(calls[0], { stepId: 's1', status: 'stopped', stoppedReason: 'user_stopped' });
    expect(handoffState.rowsFor('src').find((r) => r.handoffId === 'h1')?.stoppedReason).toBe('user_stopped');
  });

  it('sends a checklist step on the same invocation, and reads its set rows from the run once it settles (PR-D.7c)', async () => {
    const view = run();
    view.steps[2] = { ...view.steps[2], kind: 'checklist', fieldSets: ['budget', 'compliance'], stepName: 'audit', reads: ['files'] };
    runState.upsertRun(view);
    const launched = launches(view);
    expect(launched[2]).toMatchObject({ kind: 'checklist', modelId: 'us.anthropic.claude-haiku-5-5' });
    queue.enqueue(ENVELOPE, launched);
    // Prompt and checklist steps ride one invocation together; the Agent step takes its slot.
    expect(calls[0].stepIds).toEqual(['s1', 's2', 's3']);
    expect(sends.map((s) => s.handoffId)).toEqual(['ha']);

    const sets = [
      { runId: 'run1', stepId: 's3', fieldSet: 'budget', setIndex: 0, status: 'done' as const, fields: [{ name: 'cap', state: 'supported', value: '$600,000' }] },
      { runId: 'run1', stepId: 's3', fieldSet: 'compliance', setIndex: 1, status: 'error' as const, error: 'timeout', fields: [] },
    ];
    serverRun = { ...view, steps: view.steps.map((step) => (step.stepId === 's3' ? { ...step, checklist: sets } : step)) };
    serverRows.set('h3', row('h3', 'done', { kind: 'checklist', outputText: '### Budget' }));
    // A prompt step's frame reads only its row; a checklist step's also reads the run.
    frame(calls[0], { stepId: 's1', status: 'done', kind: 'prompt' });
    frame(calls[0], { stepId: 's3', status: 'done', kind: 'checklist' });
    await vi.waitFor(() => expect(runState.runsFor('src')[0].steps[2].checklist).toEqual(sets));
    expect(runReads).toEqual(['run1']);
    expect(reads).toEqual(['h1', 'h3']);
    // Nothing else on the run this tab holds was replaced by the read.
    expect(runState.runsFor('src')[0].steps[0].checklist).toBeUndefined();
  });

  it('offers a never-started prompt step as unstarted, with its kind and model', () => {
    const view = run();
    runState.upsertRun(view);
    const unstarted = queue.unstartedSteps(view);
    const summary = unstarted.find((step) => step.handoffId === 'hs')!;
    expect(summary.kind).toBe('prompt');
    expect(summary.modelId).toBe('us.anthropic.claude-sonnet-5-5');
    expect(summary.reads).toEqual(['h1', 'h2']);
  });
});
