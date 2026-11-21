import { describe, expect, it } from 'vitest';

import { HandoffView } from './handoff.models';
import { runProgress, waitingLabel } from './workflow-run-progress';
import { WorkflowRunView } from './workflow-run.models';

/** A run's lines for a step that reads others (PR-D.5a): "Waiting for X" until each has settled. */

function row(handoffId: string, status: HandoffView['status'], targetSessionId?: string): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: handoffId,
    agentName: handoffId,
    request: 'r',
    sourceMessageIndex: 0,
    excerptTokens: 0,
    createdAt: '2026-11-20T00:00:00Z',
    updatedAt: '2026-11-20T00:00:00Z',
    ...(targetSessionId ? { targetSessionId } : {}),
  };
}

function run(rows: HandoffView[], reads = ['generalist', 'colleague']): WorkflowRunView {
  const steps = [
    { stepName: 'generalist', agentName: 'Generalist' },
    { stepName: 'colleague', agentName: 'Colleague' },
    { stepName: 'summary', agentName: 'Summary', reads },
  ];
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'r',
    steps: steps.map((step, i) => ({
      stepId: `s${i + 1}`,
      handoffId: `h${i + 1}`,
      agentId: `a${i + 1}`,
      request: 'r',
      removed: false,
      ...step,
      handoff: rows[i],
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 0,
    createdAt: '2026-11-20T00:00:00Z',
    updatedAt: '2026-11-20T00:00:00Z',
  };
}

const signals = (pending: string[] = [], loading: string[] = []) => ({
  isLoading: (target: string) => loading.includes(target),
  isPending: (handoffId: string) => pending.includes(handoffId),
});

describe('workflow run progress, with stages', () => {
  it('a reader waits for the steps it reads that haven’t settled, queued or not started', () => {
    const view = run([row('h1', 'done', 't1'), row('h2', 'running', 't2'), row('h3', 'open')]);

    const queued = runProgress(view, [], signals(['h3'])).lines[2];
    expect([queued.state, queued.waitingFor]).toEqual(['queued', ['Colleague']]);

    // After a reload nothing is queued: still "Not started", and still waiting.
    const reloaded = runProgress(view, [], signals()).lines[2];
    expect([reloaded.state, reloaded.waitingFor]).toEqual(['not_started', ['Colleague']]);
  });

  it('a step waiting on the user hasn’t settled; a stopped one has', () => {
    const asking = runProgress(run([row('h1', 'needs_input', 't1'), row('h2', 'stopped', 't2'), row('h3', 'open')]), [], signals());
    expect(asking.lines[2].waitingFor).toEqual(['Generalist']);

    const settled = runProgress(run([row('h1', 'done', 't1'), row('h2', 'stopped', 't2'), row('h3', 'open')]), [], signals());
    expect(settled.lines[2]).toMatchObject({ state: 'not_started', waitingFor: [] });
  });

  it('a done row still streaming in this tab hasn’t settled yet', () => {
    const view = run([row('h1', 'done', 't1'), row('h2', 'done', 't2'), row('h3', 'open')]);
    expect(runProgress(view, [], signals([], ['t2'])).lines[2].waitingFor).toEqual(['Colleague']);
  });

  it('a run of mentions waits on nothing', () => {
    const view = run([row('h1', 'running', 't1'), row('h2', 'running', 't2'), row('h3', 'open')], []);
    expect(runProgress(view, [], signals(['h3'])).lines.map((line) => line.waitingFor)).toEqual([[], [], []]);
  });

  it('names one, two or more Agents', () => {
    expect(waitingLabel(['Generalist'])).toBe('Waiting for Generalist');
    expect(waitingLabel(['Generalist', 'Colleague'])).toBe('Waiting for Generalist and Colleague');
    expect(waitingLabel(['A', 'B', 'C'])).toBe('Waiting for A, B and C');
  });
});
