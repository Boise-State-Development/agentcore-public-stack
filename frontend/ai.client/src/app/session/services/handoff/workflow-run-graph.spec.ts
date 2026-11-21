import { describe, expect, it } from 'vitest';

import { HandoffStatus, HandoffView } from './handoff.models';
import { elapsedBetween, graphStages, reviewGraph, runGraph, runReadsLabel } from './workflow-run-graph';
import { runProgress } from './workflow-run-progress';
import { RunReview, WorkflowRunView } from './workflow-run.models';

const STEPS = [
  { name: 'generalist', agent: 'Generalist', reads: [] as string[] },
  { name: 'colleague', agent: 'Colleague', reads: [] },
  { name: 'fiscal', agent: 'Fiscal outsider', reads: [] },
  { name: 'summary', agent: 'Summarizer', reads: ['generalist', 'colleague', 'fiscal'] },
];

function row(i: number, status: HandoffStatus, updatedAt = ''): HandoffView {
  return {
    handoffId: `h${i}`,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: `a${i}`,
    agentName: STEPS[i].agent,
    request: 'r',
    sourceMessageIndex: 1,
    excerptTokens: 1,
    createdAt: '',
    updatedAt,
    runId: 'run1',
    stepId: `s${i}`,
    targetSessionId: status === 'open' ? undefined : `t${i}`,
  };
}

function stagedRun(statuses: HandoffStatus[], extra: Partial<WorkflowRunView> = {}): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'r',
    steps: STEPS.map((step, i) => ({
      stepId: `s${i}`,
      handoffId: `h${i}`,
      agentId: `a${i}`,
      agentName: step.agent,
      request: 'r',
      removed: false,
      stepName: step.name,
      reads: step.reads.length ? step.reads : undefined,
      handoff: row(i, statuses[i], '2026-11-21T10:01:04Z'),
    })),
    fileIds: ['f1'],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 1,
    createdAt: '',
    updatedAt: '',
    approvedAt: '2026-11-21T10:00:00Z',
    workflow: { skillId: 'sk', name: 'review', title: 'Review', fingerprint: 'f', resultReads: ['summary'], resultMode: 'merge', resultInstruction: 'One review.' },
    ...extra,
  };
}

const signals = { isLoading: () => false, isPending: () => false };

describe('the run graph', () => {
  it('lays the steps out by what they read, with the reads and the Result as edges', () => {
    const run = stagedRun(['done', 'done', 'running', 'open']);
    const graph = runGraph(run, runProgress(run, [], signals), (line) => `phrase ${line.step.agentName}`);

    expect(graph.stages.map((stage) => stage.map((card) => card.title))).toEqual([
      ['Generalist', 'Colleague', 'Fiscal outsider'],
      ['Summarizer'],
    ]);
    expect(graph.edges).toEqual([
      ['start', 's0'], ['start', 's1'], ['start', 's2'],
      ['s0', 's3'], ['s1', 's3'], ['s2', 's3'],
      ['s3', 'result'],
    ]);
    expect(graph.stages[1][0].readsLabel).toBe('reads the request, the files and 3 results');
    expect(graph.status.get('s0')).toEqual({ state: 'done', phrase: 'phrase Generalist', elapsed: '1m 04s' });
    expect(graph.status.get('s3')?.state).toBe('not_started');
    expect([...graph.finished]).toEqual(['start', 's0', 's1']);
    expect(graph.result).toEqual({ title: 'Merged in this conversation', meta: '2 of 4 finished' });
    expect(graph.start.meta).toBe('Started · the request and the files');
  });

  it('a run of mentions is one stage, and its Result takes every step as one card', () => {
    const run = stagedRun(['done', 'done', 'done', 'done'], { workflow: undefined, fileIds: [], resultDeliveryId: 'd1' });
    run.steps.forEach((step) => {
      delete step.stepName;
      delete step.reads;
    });
    const graph = runGraph(run, runProgress(run, [], signals), () => '');

    expect(graph.stages).toHaveLength(1);
    expect(graph.edges.filter(([, to]) => to === 'result')).toHaveLength(4);
    expect(graph.result).toEqual({ title: 'One card', meta: 'Results ready · 4 of 4 finished' });
  });

  it('the sheet’s graph drops removed steps, and says when each kept one would start', () => {
    const review = {
      draft: { agents: [], request: '' },
      phase: 'ready',
      request: 'r',
      card: '',
      cardRemoved: false,
      fileIds: [],
      steps: STEPS.map((step, i) => ({
        stepId: `s${i}`,
        handoffId: `h${i}`,
        agentId: `a${i}`,
        agentName: step.agent,
        ownRequest: null,
        removed: i === 2,
        workflowStep: { name: step.name, readsSteps: step.reads, unavailable: false, changed: false, runsDraft: false },
      })),
      workflow: { ref: { skillId: 'sk', name: 'review' }, document: { workflow: { result: { mode: 'merge', reads: ['summary'], steps: ['summary'] } } } },
    } as unknown as RunReview;

    const graph = reviewGraph(review, () => 'Sonnet 5.5');
    expect(graph.stages.map((stage) => stage.map((card) => card.key))).toEqual([['s0', 's1'], ['s3']]);
    expect(graph.status.get('s0')).toEqual({ state: 'queued', phrase: 'Starts first · Sonnet 5.5' });
    expect(graph.status.get('s3')?.phrase).toBe('After Generalist and Colleague · Sonnet 5.5');
    expect(graph.result).toEqual({ title: 'Merged in this conversation', meta: '1 result' });
    expect(graph.finished.size).toBe(0);
    expect(graph.start.meta).toBe('The request');
  });

  it('helpers', () => {
    expect(graphStages([{ key: 'a', name: 'a', reads: ['b'] }, { key: 'b', name: 'b', reads: ['a'] }]).flat().sort()).toEqual(['a', 'b']);
    expect(graphStages([])).toEqual([[]]);
    expect(runReadsLabel(false, 0)).toBe('reads the request');
    expect(runReadsLabel(false, 1)).toBe('reads the request and 1 result');
    expect(elapsedBetween('2026-11-21T10:00:00Z', '2026-11-21T10:00:09Z')).toBe('9s');
    expect(elapsedBetween(undefined, 'x')).toBeUndefined();
  });
});
