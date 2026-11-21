/**
 * Where a workflow run stands, step by step (docs/specs/workflow-runs.md §8, §20.2), as pure
 * functions over what the SPA already holds: the run row, each step's handoff row (the live one
 * this tab keeps wins over the run's replayed copy), whether its conversation streams in this tab,
 * and whether it waits in this tab's queue. The run's row, its result and its toast all read these,
 * so they can never disagree about a step.
 */

import { HandoffView } from './handoff.models';
import { WorkflowRunStepView, WorkflowRunView } from './workflow-run.models';

/**
 * A step as the user sees it.
 *
 * - `queued`: approved, waiting in this tab's queue for one of the four slots.
 * - `not_started`: approved, never started, and not queued here (a reload ended the tab's queue).
 * - `running` / `needs_input` / `done` / `stopped`: the handoff row's own states. A step of a
 *   stopped run that never started, a dismissed or abandoned row, and a row that is gone read
 *   `stopped`, as the run's result names them (§19 PR-W.2 "Part statuses").
 */
export type RunStepState = 'queued' | 'not_started' | 'running' | 'needs_input' | 'done' | 'stopped';

export interface RunStepLine {
  step: WorkflowRunStepView;
  /** The step's handoff row, live if this tab has one; `null` when the row is gone. */
  row: HandoffView | null;
  state: RunStepState;
  /**
   * A step that reads other steps and hasn't started (`queued` or `not_started`): the Agents of the
   * steps it reads that haven't settled yet, in step order (PR-D.5a). Empty or absent once it
   * could start; optional so a line built elsewhere (the graph's run mode) needs no change.
   */
  waitingFor?: string[];
}

export interface RunProgress {
  /** One line per kept step, in step order. */
  lines: RunStepLine[];
  /** Kept steps. */
  total: number;
  done: number;
  needsInput: number;
  /** Every kept step is `done` or `stopped`: nothing is left to wait for. */
  settled: boolean;
  /** Some step is queued, running or waiting on the user, so Stop run has something to stop. */
  active: boolean;
}

/** What a step's state reads that the run row doesn't hold. */
export interface RunStepSignals {
  /** The step's conversation is streaming in this tab. */
  isLoading(targetSessionId: string): boolean;
  /** The step waits in this tab's queue. */
  isPending(handoffId: string): boolean;
}

/** The step's handoff row: this tab's live copy when it has one, else the run's replayed copy. */
export function stepRow(step: WorkflowRunStepView, liveRows: readonly HandoffView[]): HandoffView | null {
  return liveRows.find((row) => row.handoffId === step.handoffId) ?? step.handoff ?? null;
}

export function stepState(run: WorkflowRunView, row: HandoffView | null, step: WorkflowRunStepView, signals: RunStepSignals): RunStepState {
  if (!row) return 'stopped';
  if (row.targetSessionId && signals.isLoading(row.targetSessionId)) return 'running';
  switch (row.status) {
    case 'open':
      if (row.targetSessionId) return 'running';
      if (run.status !== 'approved') return 'stopped';
      return signals.isPending(step.handoffId) ? 'queued' : 'not_started';
    case 'running':
    case 'needs_input':
    case 'done':
      return row.status;
    default:
      return 'stopped';
  }
}

/** The steps `step` reads (PR-D.5a), in step order. None on a run of mentions. */
export function readStepsOf(run: WorkflowRunView, step: WorkflowRunStepView): WorkflowRunStepView[] {
  const names = new Set(step.reads ?? []);
  if (names.size === 0) return [];
  return run.steps.filter((candidate) => candidate.stepId !== step.stepId && !!candidate.stepName && names.has(candidate.stepName));
}

/**
 * A step a later one reads has settled: it is done or stopped (a dropped, failed or never-started
 * step of a stopped run included), so it will give nothing more. The reader starts then, whatever
 * the outcome, and its first message names a step that stopped, as the run's result does.
 */
export function readSettled(run: WorkflowRunView, read: WorkflowRunStepView, liveRows: readonly HandoffView[], signals: RunStepSignals): boolean {
  if (read.removed) return true;
  const state = stepState(run, stepRow(read, liveRows), read, signals);
  return state === 'done' || state === 'stopped';
}

/** "Waiting for Generalist", "… for Generalist and Colleague", "… for A, B and C". */
export function waitingLabel(agentNames: readonly string[]): string {
  const names = agentNames.map((name) => name.trim() || 'another step');
  if (names.length <= 1) return `Waiting for ${names[0] ?? 'another step'}`;
  return `Waiting for ${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

export function runProgress(run: WorkflowRunView, liveRows: readonly HandoffView[], signals: RunStepSignals): RunProgress {
  const lines = run.steps
    .filter((step) => !step.removed)
    .map((step): RunStepLine => {
      const row = stepRow(step, liveRows);
      const state = stepState(run, row, step, signals);
      const waitingFor =
        state === 'queued' || state === 'not_started'
          ? readStepsOf(run, step)
              .filter((read) => !readSettled(run, read, liveRows, signals))
              .map((read) => read.agentName)
          : [];
      return { step, row, state, waitingFor };
    });
  const done = lines.filter((line) => line.state === 'done').length;
  const needsInput = lines.filter((line) => line.state === 'needs_input').length;
  const settled = lines.length > 0 && lines.every((line) => line.state === 'done' || line.state === 'stopped');
  const active = lines.some((line) => line.state === 'queued' || line.state === 'running' || line.state === 'needs_input');
  return { lines, total: lines.length, done, needsInput, settled, active };
}

/** Whether a run has a row in the thread: once approved, whatever happened after. */
export function showsInThread(run: WorkflowRunView): boolean {
  return run.status === 'approved' || run.status === 'stopped';
}
