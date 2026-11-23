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
  /**
   * A prompt step is on this tab's prompt invocation and its frame hasn't arrived (PR-D.6c): it
   * reads running, though its row still says `open` until the frame says how it ended.
   */
  isRunning?(handoffId: string): boolean;
}

/** The step's handoff row: this tab's live copy when it has one, else the run's replayed copy. */
export function stepRow(step: WorkflowRunStepView, liveRows: readonly HandoffView[]): HandoffView | null {
  return liveRows.find((row) => row.handoffId === step.handoffId) ?? step.handoff ?? null;
}

export function stepState(run: WorkflowRunView, row: HandoffView | null, step: WorkflowRunStepView, signals: RunStepSignals): RunStepState {
  if (!row) return 'stopped';
  if (isModelRunStep(step) || isModelStepKind(row.kind)) return promptStepState(run, row, step, signals);
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

/** A step with no Agent and no conversation: a prompt step (§21.3) or a checklist step (§22.3). */
export type ModelStepKind = 'prompt' | 'checklist';

/** `kind` names a prompt or a checklist step, the two kinds of step that are model calls alone. */
export function isModelStepKind(kind: string | null | undefined): kind is ModelStepKind {
  return kind === 'prompt' || kind === 'checklist';
}

/**
 * A workflow run's model step: a prompt step (workflow-designer §21.3, one model call) or a
 * checklist step (§22.3, one call per field set). Neither has an Agent or a conversation, so both
 * ride the run's `workflow_prompt` invocation and are read from their own row.
 */
export function isModelRunStep(step: Pick<WorkflowRunStepView, 'kind'>): boolean {
  return isModelStepKind(step.kind);
}

/** A workflow run's checklist step (§22.3): one model call per field set, read as a table. */
export function isChecklistRunStep(step: Pick<WorkflowRunStepView, 'kind'>): boolean {
  return step.kind === 'checklist';
}

/**
 * A prompt or checklist step has no conversation, so nothing streams for it: its state is its own row's
 * (`open → running → done | stopped`), and while this tab's invocation holds it, running.
 */
function promptStepState(run: WorkflowRunView, row: HandoffView, step: WorkflowRunStepView, signals: RunStepSignals): RunStepState {
  switch (row.status) {
    case 'open':
      if (signals.isRunning?.(step.handoffId)) return 'running';
      if (run.status !== 'approved') return 'stopped';
      return signals.isPending(step.handoffId) ? 'queued' : 'not_started';
    case 'running':
    case 'done':
      return row.status;
    default:
      return 'stopped';
  }
}

/** The steps `step` reads (PR-D.5a), in step order. None on a run of mentions. */
export function readStepsOf(run: WorkflowRunView, step: WorkflowRunStepView): WorkflowRunStepView[] {
  // A prompt step's reads also name its inputs (`request`, `files`), which no step is called.
  const names = new Set((step.reads ?? []).filter((name) => name !== 'request' && name !== 'files'));
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

/**
 * A saved step's name as a person reads it: "fiscal_outsider" → "Fiscal outsider". A prompt step
 * is called this, since several can share one model (§21.3).
 */
export function stepNameLabel(name: string | undefined): string {
  const words = (name ?? '').replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : '';
}

/**
 * "Prompt · Haiku 5.5", or "Checklist · Haiku 5.5": a model step's worker, named by its kind and
 * its model. Absent `kind` reads as a prompt step, as every caller before checklist steps meant.
 */
export function promptWorkerLabel(modelName: string | undefined, kind: string | null | undefined = 'prompt'): string {
  return `${kind === 'checklist' ? 'Checklist' : 'Prompt'} · ${modelName?.trim() || 'a model'}`;
}

/** "1 field set", "4 field sets". */
export function fieldSetsLabel(count: number): string {
  return `${count} ${count === 1 ? 'field set' : 'field sets'}`;
}

/**
 * Why one of a checklist step's field sets has no table (`error` on its `WFCHK#` row, §22.3), in
 * plain words. A set that fails alone never stops the step's other sets. A code this doesn't know
 * reads as the generic line, including `unsupported_field`, which only rows written before list
 * fields could be filled carry.
 */
export function checklistSetErrorLine(code: string | null | undefined): string {
  switch (code) {
    case 'field_set_missing':
      return 'The field set is no longer in the workflow’s skill, so it wasn’t run.';
    case 'no_tool_call':
      return 'The model answered without filling in the fields, twice.';
    case 'timeout':
      return 'It took too long, so it was stopped.';
    case 'cut_off':
      return 'The model’s answer ran past its output limit before it filled in the fields.';
    case 'model_error':
      return 'The model call failed.';
    default:
      return 'This field set couldn’t run.';
  }
}

/**
 * Why a prompt step stopped, in plain words (`stoppedReason`, §21.3). The model is named where it
 * is the reason, so the user knows what to change.
 */
export function promptStopLine(reason: string | undefined, modelName: string | undefined): string {
  const model = modelName?.trim() || 'this model';
  switch (reason) {
    case 'user_stopped':
      return 'Stopped';
    case 'too_large':
      return `Stopped · the documents are too large for ${model}`;
    case 'model_refused':
      return `Stopped · ${model} can’t run this step for you`;
    case 'quota':
      return 'Stopped · you’ve reached your usage limit';
    case 'timeout':
      return 'Stopped · it took too long';
    case 'error':
      return 'Stopped · something went wrong';
    default:
      return 'Stopped';
  }
}

/**
 * A prompt step's status line (§21.3), shared by the run's row and the designer's test run so the
 * two never word one state differently. It has no conversation, so nothing live to read.
 */
export function promptStatusLine(line: Pick<RunStepLine, 'state' | 'row' | 'waitingFor' | 'step'>): string {
  switch (line.state) {
    case 'queued':
      return line.waitingFor?.length ? waitingLabel(line.waitingFor) : 'Queued';
    case 'not_started':
      return line.waitingFor?.length ? waitingLabel(line.waitingFor) : 'Not started';
    case 'running':
      return 'Working';
    case 'done':
      return 'Finished';
    default:
      return promptStopLine(line.row?.stoppedReason, line.row?.agentName || line.step.agentName);
  }
}

/** "4 Agents", "1 Agent". */
export function agentsLabel(count: number): string {
  return `${count} ${count === 1 ? 'Agent' : 'Agents'}`;
}

/** "4 Agents", or "6 steps" once any of them is a prompt or checklist step, which has no Agent. */
export function stepsLabel(count: number, anyPrompt: boolean): string {
  if (!anyPrompt) return agentsLabel(count);
  return `${count} ${count === 1 ? 'step' : 'steps'}`;
}

/** A step as another's "Waiting for …" names it: its Agent, or a model step's own name. */
export function workerName(step: Pick<WorkflowRunStepView, 'kind' | 'agentName' | 'stepName'>): string {
  if (isModelStepKind(step.kind)) return stepNameLabel(step.stepName) || promptWorkerLabel(step.agentName, step.kind);
  return step.agentName;
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
              .map(workerName)
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
