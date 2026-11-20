import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentHandoffService, HandoffLaunch } from './agent-handoff.service';
import { HandoffStateService } from './handoff-state.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { RunEnvelope, RunStepLaunch } from './workflow-run.models';

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
    localStorage.clear();
    sends = [];
    TestBed.configureTestingModule({
      providers: [
        HandoffStateService,
        {
          provide: AgentHandoffService,
          useValue: {
            startRunStep: (step: HandoffLaunch) =>
              new Promise<void>((resolve, reject) => sends.push({ step, resolve, reject })),
          },
        },
      ],
    });
    queue = TestBed.inject(WorkflowRunQueueService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

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
    expect(queue.pendingCount('run1')).toBe(1);
    // The run stays as it was, so a reload can still start its queued step.
    expect(queue.storedEnvelope('run1')).toEqual(expect.objectContaining({ contextText: 'reviewed card' }));
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

  it('ignores an ordinary pageshow', () => {
    window.dispatchEvent(pageEvent('pageshow', false));
    queue.enqueue(ENVELOPE, STEPS);
    expect(started()).toHaveLength(4);
  });
});
