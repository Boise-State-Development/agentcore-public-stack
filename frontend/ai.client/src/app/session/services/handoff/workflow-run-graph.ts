/**
 * A run, or a sheet about to start one, as `WorkflowGraphComponent` draws it in run mode
 * (docs/specs/workflow-designer.md §7 D4, §20.3 PR-D.5b): View as graph on the run's row, and the
 * small graph on the sheet. Pure functions over what the row and the sheet already hold, so the
 * graph says exactly what the row's lines say.
 *
 * The columns follow the designer's rule (`stagesOf`): a step that reads no other step is in stage
 * 1, and one that reads others is in the stage after the latest it reads. A run of mentions reads
 * nothing, so it is one stage. Node ids are the graph's own: `start`, `result`, or a step's id.
 */

import type {
  GraphEdge,
  GraphFixedCard,
  GraphStepCard,
  GraphStepStatus,
} from '../../../workflows/components/workflow-graph.component';
import { RunProgress, RunStepLine, promptWorkerLabel, stepNameLabel } from './workflow-run-progress';
import { RunReview, WorkflowRunView } from './workflow-run.models';

export const GRAPH_START = 'start';
export const GRAPH_RESULT = 'result';

/** Everything the graph's inputs take, for one run or one sheet. */
export interface RunGraph {
  stages: GraphStepCard[][];
  edges: GraphEdge[];
  start: GraphFixedCard;
  result: GraphFixedCard;
  status: ReadonlyMap<string, GraphStepStatus>;
  finished: ReadonlySet<string>;
  stepCount: number;
}

/** One step as the layout needs it. */
interface GraphNode {
  key: string;
  name: string;
  /** Other steps' names this one reads. */
  reads: readonly string[];
  card: GraphStepCard;
}

/** Stages of node keys, each in step order; a read of a missing step is no edge. */
export function graphStages(nodes: readonly Pick<GraphNode, 'key' | 'name' | 'reads'>[]): string[][] {
  const byName = new Map(nodes.map((node) => [node.name, node]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (node: Pick<GraphNode, 'key' | 'name' | 'reads'>): number => {
    const known = depth.get(node.key);
    if (known !== undefined) return known;
    if (visiting.has(node.key)) return 0;
    visiting.add(node.key);
    let d = 0;
    for (const read of node.reads) {
      const other = byName.get(read);
      if (other && other.key !== node.key) d = Math.max(d, depthOf(other) + 1);
    }
    visiting.delete(node.key);
    depth.set(node.key, d);
    return d;
  };
  const stages: string[][] = [];
  for (const node of nodes) {
    const d = depthOf(node);
    while (stages.length <= d) stages.push([]);
    stages[d].push(node.key);
  }
  return stages.length ? stages : [[]];
}

/**
 * "reads the request, the files and 2 results": the card's edges in words (§4.4). Every Agent
 * step gets the request; a prompt step only when it reads it (`hasRequest`, §21.3).
 */
export function runReadsLabel(hasFiles: boolean, results: number, hasRequest = true): string {
  const parts: string[] = [];
  if (hasRequest) parts.push('the request');
  if (hasFiles) parts.push('the files');
  if (results) parts.push(`${results} ${results === 1 ? 'result' : 'results'}`);
  if (parts.length === 0) return 'reads nothing';
  return `reads ${parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`}`;
}

/** A prompt step's card: titled by its step's name, since several can share a model; the meta names the model. */
function promptCard(key: string, stepName: string | undefined, modelName: string, readsLabel: string): GraphStepCard {
  const worker = promptWorkerLabel(modelName);
  return { key, kind: 'prompt', title: stepNameLabel(stepName) || worker, meta: worker, agentId: null, readsLabel };
}

/** "1m 04s", from the run's approval to the step's row settling, as the designer's test run says it. */
export function elapsedBetween(from: string | undefined, to: string | undefined): string | undefined {
  if (!from || !to) return undefined;
  const ms = Date.parse(to) - Date.parse(from);
  if (!Number.isFinite(ms) || ms < 0) return undefined;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

function layout(
  nodes: readonly GraphNode[],
  resultTakes: readonly string[] | null,
): { stages: GraphStepCard[][]; edges: GraphEdge[] } {
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const byName = new Map(nodes.map((node) => [node.name, node.key]));
  const keys = graphStages(nodes);
  const edges: GraphEdge[] = (keys[0] ?? []).map((key) => [GRAPH_START, key] as const);
  for (const node of nodes) {
    for (const read of node.reads) {
      const from = byName.get(read);
      if (from && from !== node.key) edges.push([from, node.key]);
    }
  }
  const takes = resultTakes && resultTakes.some((name) => byName.has(name)) ? new Set(resultTakes) : null;
  for (const node of nodes) {
    if (takes === null || takes.has(node.name)) edges.push([node.key, GRAPH_RESULT]);
  }
  return { stages: keys.map((stage) => stage.map((key) => byKey.get(key)!.card)), edges };
}

/** The Result card's title: one card, or the merge (PR-D.5b). */
function resultTitle(merge: boolean): string {
  return merge ? 'Merged in this conversation' : 'One card';
}

/**
 * A started run on its row (View as graph). Each step's status is its line's, worded by
 * `phrase`, which the row passes so the graph and the line say the same thing.
 */
export function runGraph(run: WorkflowRunView, progress: RunProgress, phrase: (line: RunStepLine) => string): RunGraph {
  const hasFiles = run.fileIds.length > 0;
  const nodes = progress.lines.map((line): GraphNode => {
    const reads = (line.step.reads ?? []).filter((name) => progress.lines.some((other) => other.step.stepName === name));
    const own = line.step.reads;
    const card: GraphStepCard =
      line.step.kind === 'prompt'
        ? promptCard(
            line.step.stepId,
            line.step.stepName,
            line.step.agentName,
            // A prompt row's reads keep its inputs; one written without any reads both (§21.3).
            runReadsLabel(hasFiles && (!own || own.includes('files')), reads.length, !own || own.includes('request')),
          )
        : {
            key: line.step.stepId,
            title: line.step.agentName,
            meta: 'Agent',
            agentId: line.step.agentId,
            readsLabel: runReadsLabel(hasFiles, reads.length),
          };
    return { key: line.step.stepId, name: line.step.stepName ?? line.step.stepId, reads, card };
  });
  const { stages, edges } = layout(nodes, run.workflow?.resultReads ?? null);

  const status = new Map<string, GraphStepStatus>();
  const finished = new Set<string>([GRAPH_START]);
  for (const line of progress.lines) {
    status.set(line.step.stepId, {
      state: line.state,
      phrase: phrase(line),
      elapsed: line.state === 'done' ? elapsedBetween(run.approvedAt, line.row?.updatedAt) : undefined,
    });
    if (line.state === 'done') finished.add(line.step.stepId);
  }

  const merge = run.workflow?.resultMode === 'merge';
  const count = `${progress.done} of ${progress.total} finished`;
  return {
    stages,
    edges,
    start: { title: 'Starts with', meta: hasFiles ? 'Started · the request and the files' : 'Started · the request' },
    result: { title: resultTitle(merge), meta: run.resultDeliveryId ? `Results ready · ${count}` : count },
    status,
    finished,
    stepCount: progress.total,
  };
}

/**
 * The sheet's small graph: the steps it would start, before Start. Nothing has run, so every
 * step reads as not started yet, and its phrase says when it would start.
 */
export function reviewGraph(review: RunReview, modelName: (stepId: string) => string): RunGraph {
  const kept = review.steps.filter((step) => !step.removed);
  const hasFiles = review.fileIds.length > 0;
  const keptNames = new Set(kept.map((step) => step.workflowStep?.name).filter((name): name is string => !!name));
  const nodes = kept.map((step): GraphNode => {
    const reads = (step.workflowStep?.readsSteps ?? []).filter((name) => keptNames.has(name));
    const card: GraphStepCard =
      step.kind === 'prompt'
        ? promptCard(
            step.stepId,
            step.workflowStep?.name,
            step.agentName,
            runReadsLabel(hasFiles && (step.workflowStep?.readsFiles ?? true), reads.length, readsRequest(review, step.workflowStep?.name)),
          )
        : {
            key: step.stepId,
            title: step.agentName || step.workflowStep?.name || 'A step',
            meta: 'Agent',
            agentId: step.agentId,
            readsLabel: runReadsLabel(hasFiles, reads.length),
          };
    return { key: step.stepId, name: step.workflowStep?.name ?? step.stepId, reads, card };
  });
  const result = review.workflow?.document.workflow.result;
  const { stages, edges } = layout(nodes, result?.reads ?? null);

  const agentOf = new Map(
    kept.map((step) => [
      step.workflowStep?.name ?? step.stepId,
      step.kind === 'prompt' ? stepNameLabel(step.workflowStep?.name) || step.agentName : step.agentName,
    ]),
  );
  const status = new Map<string, GraphStepStatus>();
  for (const node of nodes) {
    const after = node.reads.map((name) => agentOf.get(name) || 'another step');
    const when = after.length ? `After ${joinNames(after)}` : 'Starts first';
    status.set(node.key, { state: 'queued', phrase: `${when} · ${modelName(node.key)}` });
  }

  const merge = result?.mode === 'merge';
  const takes = edges.filter(([, to]) => to === GRAPH_RESULT).length;
  return {
    stages,
    edges,
    start: { title: 'Starts with', meta: hasFiles ? 'The request and the files' : 'The request' },
    result: { title: resultTitle(merge), meta: `${takes} ${takes === 1 ? 'result' : 'results'}` },
    status,
    finished: new Set(),
    stepCount: kept.length,
  };
}

/** A saved prompt step reads the run's request only when its reads name it; an Agent step always does. */
function readsRequest(review: RunReview, name: string | undefined): boolean {
  const step = name ? review.workflow?.document.workflow.steps.find((candidate) => candidate.name === name) : undefined;
  return !step || step.kind !== 'prompt' || step.reads.includes('request');
}

/** "A", "A and B", "A, B and C". */
function joinNames(names: readonly string[]): string {
  if (names.length <= 1) return names[0] ?? '';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}
