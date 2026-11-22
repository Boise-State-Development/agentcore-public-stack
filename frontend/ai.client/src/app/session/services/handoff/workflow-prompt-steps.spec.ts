import { describe, expect, it } from 'vitest';

import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import { promptModelChoices, promptModelSupported } from '../../../workflows/prompt-models';
import { HandoffView } from './handoff.models';
import { estimateRunInputUsd, tokenizerFactor } from './workflow-run-estimate';
import { runGraph } from './workflow-run-graph';
import {
  promptStatusLine,
  promptStopLine,
  readStepsOf,
  runProgress,
  stepNameLabel,
  stepsLabel,
  workerName,
} from './workflow-run-progress';
import { WorkflowRunStepView, WorkflowRunView } from './workflow-run.models';

/** Prompt steps (workflow-designer §21.3, PR-D.6c) in the run's pure functions. */

function promptRow(handoffId: string, status: HandoffView['status'], patch: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: 'prompt',
    status,
    agentId: '',
    agentName: 'Haiku 5.5',
    request: 'r',
    sourceMessageIndex: 0,
    excerptTokens: 0,
    createdAt: '2026-11-21T00:00:00Z',
    updatedAt: '2026-11-21T00:00:00Z',
    ...patch,
  };
}

function promptStep(i: number, reads: string[], row: HandoffView | null): WorkflowRunStepView {
  return {
    stepId: `s${i}`,
    handoffId: `h${i}`,
    kind: 'prompt',
    agentId: '',
    agentName: 'Haiku 5.5',
    modelId: 'us.anthropic.claude-haiku-5-5',
    request: 'r',
    removed: false,
    stepName: i === 3 ? 'summary' : `lens_${i}`,
    reads,
    handoff: row,
  };
}

function run(rows: (HandoffView | null)[], status: WorkflowRunView['status'] = 'approved'): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status,
    request: 'r',
    steps: [
      promptStep(1, ['request', 'files'], rows[0]),
      promptStep(2, ['request', 'files'], rows[1]),
      promptStep(3, ['lens_1', 'lens_2'], rows[2]),
    ],
    fileIds: ['f1'],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 0,
    createdAt: '2026-11-21T00:00:00Z',
    updatedAt: '2026-11-21T00:00:00Z',
    approvedAt: '2026-11-21T00:00:00Z',
  };
}

const signals = (pending: string[] = [], running: string[] = []) => ({
  isLoading: () => false,
  isPending: (id: string) => pending.includes(id),
  isRunning: (id: string) => running.includes(id),
});

describe('a prompt step’s state', () => {
  it('reads its own row: no conversation, so nothing streams for it', () => {
    const view = run([promptRow('h1', 'done'), promptRow('h2', 'running'), promptRow('h3', 'open')]);
    const progress = runProgress(view, [], signals(['h3']));
    expect(progress.lines.map((line) => line.state)).toEqual(['done', 'running', 'queued']);
    expect(progress.settled).toBe(false);
    // The summary waits for the lens still running, named by its step.
    expect(progress.lines[2].waitingFor).toEqual(['Lens 2']);
  });

  it('is running while this tab’s invocation holds it, though its row still says open', () => {
    const view = run([promptRow('h1', 'open'), promptRow('h2', 'open'), promptRow('h3', 'open')]);
    expect(runProgress(view, [], signals([], ['h1'])).lines[0].state).toBe('running');
    expect(runProgress(view, [], signals()).lines[0].state).toBe('not_started');
  });

  it('settles a run once every prompt step is done or stopped', () => {
    const view = run([promptRow('h1', 'done'), promptRow('h2', 'stopped', { stoppedReason: 'too_large' }), promptRow('h3', 'done')]);
    const progress = runProgress(view, [], signals());
    expect(progress.settled).toBe(true);
    expect(progress.done).toBe(2);
  });

  it('reads stopped for a never-claimed step of a stopped run', () => {
    const view = run([promptRow('h1', 'open'), promptRow('h2', 'done'), promptRow('h3', 'done')], 'stopped');
    expect(runProgress(view, [], signals()).lines[0].state).toBe('stopped');
  });

  it('waits on the steps it reads, never on its inputs', () => {
    const view = run([null, null, null]);
    expect(readStepsOf(view, view.steps[0])).toEqual([]);
    expect(readStepsOf(view, view.steps[2]).map((step) => step.handoffId)).toEqual(['h1', 'h2']);
  });
});

describe('a prompt step in words', () => {
  it('names a stop reason plainly, with the model where it is the reason', () => {
    expect(promptStopLine('too_large', 'Haiku 5.5')).toBe('Stopped · the documents are too large for Haiku 5.5');
    expect(promptStopLine('model_refused', 'Haiku 5.5')).toBe('Stopped · Haiku 5.5 can’t run this step for you');
    expect(promptStopLine('quota', 'x')).toBe('Stopped · you’ve reached your usage limit');
    expect(promptStopLine('timeout', 'x')).toBe('Stopped · it took too long');
    expect(promptStopLine('user_stopped', 'x')).toBe('Stopped');
    expect(promptStopLine(undefined, 'x')).toBe('Stopped');
  });

  it('a status line per state', () => {
    const view = run([promptRow('h1', 'stopped', { stoppedReason: 'too_large', agentName: 'Sonnet 5.5' }), null, null]);
    const step = view.steps[0];
    expect(promptStatusLine({ state: 'running', row: null, step })).toBe('Working');
    expect(promptStatusLine({ state: 'done', row: null, step })).toBe('Finished');
    expect(promptStatusLine({ state: 'queued', row: null, step, waitingFor: ['Lens 1'] })).toBe('Waiting for Lens 1');
    expect(promptStatusLine({ state: 'stopped', row: view.steps[0].handoff ?? null, step })).toBe(
      'Stopped · the documents are too large for Sonnet 5.5',
    );
  });

  it('labels', () => {
    expect(stepNameLabel('fiscal_outsider')).toBe('Fiscal outsider');
    expect(stepsLabel(6, true)).toBe('6 steps');
    expect(stepsLabel(1, true)).toBe('1 step');
    expect(stepsLabel(4, false)).toBe('4 Agents');
    expect(workerName({ kind: 'prompt', agentName: 'Haiku 5.5', stepName: 'summary' })).toBe('Summary');
    expect(workerName({ kind: 'prompt', agentName: 'Haiku 5.5' })).toBe('Prompt · Haiku 5.5');
    expect(workerName({ agentName: 'Generalist', stepName: 'generalist' })).toBe('Generalist');
  });
});

describe('the run sheet’s estimate for a prompt step', () => {
  const rate = 1; // USD per million input tokens
  it('counts no card, and the files only when the step reads them', () => {
    const card = 'c'.repeat(4000); // 1,000 tokens
    const base = { modelId: 'us.anthropic.claude-haiku-5-5', inputPricePerMillionTokens: rate, request: '' };
    const agent = estimateRunInputUsd({ steps: [base], card, fileTokens: [10_000] })!;
    const prompt = estimateRunInputUsd({ steps: [{ ...base, card: false }], card, fileTokens: [10_000] })!;
    const noFiles = estimateRunInputUsd({ steps: [{ ...base, card: false, files: false }], card, fileTokens: [10_000] })!;
    const factor = tokenizerFactor(base.modelId);
    expect(agent).toBeCloseTo((11_000 * factor * rate) / 1_000_000);
    expect(prompt).toBeCloseTo((10_000 * factor * rate) / 1_000_000);
    expect(noFiles).toBe(0);
  });

  it('a step that reads no files is priced while a file is still being prepared', () => {
    const steps = [{ modelId: 'm', inputPricePerMillionTokens: rate, request: 'r'.repeat(40), card: false, files: false }];
    expect(estimateRunInputUsd({ steps, card: '', fileTokens: [null] })).not.toBeNull();
    expect(estimateRunInputUsd({ steps: [{ ...steps[0], files: true }], card: '', fileTokens: [null] })).toBeNull();
  });
});

describe('the run graph with prompt steps', () => {
  it('titles a prompt card by its step, names the model, and counts only the inputs it reads', () => {
    const view = run([promptRow('h1', 'done'), promptRow('h2', 'running'), promptRow('h3', 'open')]);
    const progress = runProgress(view, [], signals());
    const graph = runGraph(view, progress, () => 'phrase');
    const cards = graph.stages.flat();
    expect(cards.map((card) => [card.kind, card.title, card.meta])).toEqual([
      ['prompt', 'Lens 1', 'Prompt · Haiku 5.5'],
      ['prompt', 'Lens 2', 'Prompt · Haiku 5.5'],
      ['prompt', 'Summary', 'Prompt · Haiku 5.5'],
    ]);
    expect(cards[0].readsLabel).toBe('reads the request and the files');
    expect(cards[2].readsLabel).toBe('reads 2 results');
    expect(graph.stages).toHaveLength(2);
  });
});

describe('the models a prompt step can run on', () => {
  const model = (patch: Partial<ManagedModel>): ManagedModel =>
    ({
      modelId: 'm',
      modelName: 'M',
      provider: 'bedrock',
      inputModalities: ['TEXT', 'IMAGE'],
      outputModalities: ['TEXT'],
      ...patch,
    }) as ManagedModel;

  it('mirrors the Runtime’s rule: Converse, text in, text alone out', () => {
    expect(promptModelSupported(model({}))).toBe(true);
    expect(promptModelSupported(model({ provider: 'openai' }))).toBe(false);
    expect(promptModelSupported(model({ outputModalities: ['SPEECH', 'TEXT'] }))).toBe(false);
    expect(promptModelSupported(model({ inputModalities: ['IMAGE'] }))).toBe(false);
  });

  it('lists a text-only model as one that can’t be sent documents, and leaves out a retiring one', () => {
    const choices = promptModelChoices([
      model({ modelId: 'a', modelName: 'Claude' }),
      model({ modelId: 'b', modelName: 'Micro', inputModalities: ['TEXT'] }),
      model({ modelId: 'c', modelName: 'Old', status: 'deprecated' } as Partial<ManagedModel>),
    ]);
    expect(choices).toEqual([
      { modelId: 'a', name: 'Claude', readsDocuments: true },
      { modelId: 'b', name: 'Micro', readsDocuments: false },
    ]);
  });
});
