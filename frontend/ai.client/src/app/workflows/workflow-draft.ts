/**
 * The designer's draft and the pure rules it is edited by (docs/specs/workflow-designer.md §4.1).
 *
 * A draft is the JSON the designer edits, plus a local key per step so a card keeps its identity
 * (and its focus) across a rename or a move. It is turned into a `PUT` body only when every step
 * has an Agent: Agent ids are per deployment, so a template's steps start with none and nothing
 * is saved until each is chosen.
 *
 * No coordinates anywhere. A step's stage is derived from what it reads (D2), and within a stage
 * steps keep their authored order.
 */

import {
  INPUT_READS,
  MAX_WORKFLOW_STEPS,
  WorkflowAgentView,
  WorkflowDocument,
  WorkflowFiles,
  WorkflowResultMode,
  WorkflowView,
  WorkflowWrite,
} from './workflow.models';

export interface DraftStep {
  /** Local identity, never sent. */
  key: string;
  /** The step's name in the file, and what other steps' reads name. */
  name: string;
  /** Renamed after its Agent when one is picked (a blank step's placeholder name). */
  autoName: boolean;
  agentId: string | null;
  agentVersion: number | null;
  reads: string[];
  /** This step's own request; null takes the run's. */
  ownRequest: string | null;
}

export interface WorkflowDraft {
  title: string;
  files: WorkflowFiles;
  request: string;
  steps: DraftStep[];
  result: { mode: WorkflowResultMode; reads: string[] | null; instruction: string | null };
}

let keySeq = 0;
function nextKey(): string {
  keySeq += 1;
  return `s${keySeq}`;
}

export function blankDraft(title = 'Untitled workflow'): WorkflowDraft {
  return { title, files: 'any', request: '', steps: [], result: { mode: 'card', reads: null, instruction: null } };
}

/** The one template (§8.2): the worked example's shape, four reviewers and a result, no Agents yet. */
export const PROPOSAL_REVIEW_TEMPLATE = 'proposal-review';

export function templateDraft(template: string): WorkflowDraft | null {
  if (template !== PROPOSAL_REVIEW_TEMPLATE) return null;
  const step = (name: string): DraftStep => ({
    key: nextKey(),
    name,
    autoName: false,
    agentId: null,
    agentVersion: null,
    reads: [...INPUT_READS],
    ownRequest: null,
  });
  return {
    title: 'Proposal review',
    files: 'one',
    request: 'Review this proposal against the solicitation.',
    steps: [step('generalist'), step('colleague'), step('emotional_outsider'), step('fiscal_outsider')],
    result: { mode: 'card', reads: null, instruction: null },
  };
}

export function draftFrom(view: WorkflowView): WorkflowDraft {
  return {
    title: view.title,
    files: view.files,
    request: view.request ?? '',
    steps: view.steps.map((step) => ({
      key: nextKey(),
      name: step.name,
      autoName: false,
      agentId: step.agentId,
      agentVersion: step.agentVersion ?? null,
      reads: [...step.reads],
      ownRequest: step.request ?? null,
    })),
    result: {
      mode: view.result.mode === 'merge' ? 'merge' : 'card',
      reads: view.result.reads ? [...view.result.reads] : null,
      instruction: view.result.instruction ?? null,
    },
  };
}

/**
 * A draft from the server's answer to a save, keeping the keys of `previous` by step name, so the
 * selected card and its focus survive the save.
 */
export function rekeyed(fresh: WorkflowDraft, previous: WorkflowDraft): WorkflowDraft {
  const keys = new Map(previous.steps.map((s) => [s.name, s.key]));
  return { ...fresh, steps: fresh.steps.map((s) => ({ ...s, key: keys.get(s.name) ?? s.key })) };
}

/** Every step has an Agent, so the draft can be sent. */
export function isComplete(draft: WorkflowDraft): boolean {
  return draft.steps.every((step) => !!step.agentId);
}

/**
 * The `PUT` body. A step with no recorded version takes the Agent's published version as the
 * server last reported it (`agents`), which is "the version when the step was added" (D6).
 * Null while any step has no Agent.
 */
export function toWrite(
  draft: WorkflowDraft,
  agents: ReadonlyMap<string, WorkflowAgentView> = new Map(),
  expectedVersion?: number,
): WorkflowWrite | null {
  if (!isComplete(draft)) return null;
  const body: WorkflowWrite = {
    title: draft.title.trim() || 'Untitled workflow',
    files: draft.files,
    request: draft.request.trim() ? draft.request : null,
    steps: draft.steps.map((step) => ({
      name: step.name,
      kind: 'agent',
      agentId: step.agentId as string,
      agentVersion: step.agentVersion ?? agents.get(step.agentId as string)?.publishedVersion ?? null,
      reads: [...step.reads],
      request: step.ownRequest !== null && step.ownRequest.trim() ? step.ownRequest : null,
    })),
    result: {
      mode: draft.result.mode,
      reads: draft.result.reads,
      instruction: draft.result.instruction?.trim() ? draft.result.instruction : null,
    },
  };
  if (expectedVersion !== undefined) body.expectedVersion = expectedVersion;
  return body;
}

/** Whether two drafts would save the same definition (the dirty check; keys are ignored). */
export function sameDraft(a: WorkflowDraft, b: WorkflowDraft): boolean {
  const strip = (d: WorkflowDraft) => ({
    title: d.title.trim(),
    files: d.files,
    request: d.request.trim(),
    steps: d.steps.map((s) => ({
      name: s.name,
      agentId: s.agentId,
      agentVersion: s.agentVersion,
      reads: [...s.reads].sort(),
      ownRequest: s.ownRequest?.trim() || null,
    })),
    result: { mode: d.result.mode, reads: d.result.reads, instruction: d.result.instruction?.trim() || null },
  });
  return JSON.stringify(strip(a)) === JSON.stringify(strip(b));
}

/**
 * Stages, each a list of step keys in authored order (D2): a step that reads no other step is in
 * stage 1, and one that reads another is in the stage after the latest one it reads. Reads the
 * format refuses (a cycle, a missing step) never reach here, but are read as no edge rather than
 * looping.
 */
export function stagesOf(steps: readonly DraftStep[]): string[][] {
  const byName = new Map(steps.map((step) => [step.name, step]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (step: DraftStep): number => {
    const known = depth.get(step.key);
    if (known !== undefined) return known;
    if (visiting.has(step.key)) return 0;
    visiting.add(step.key);
    let d = 0;
    for (const read of step.reads) {
      const other = byName.get(read);
      if (other && other.key !== step.key) d = Math.max(d, depthOf(other) + 1);
    }
    visiting.delete(step.key);
    depth.set(step.key, d);
    return d;
  };
  const stages: string[][] = [];
  for (const step of steps) {
    const d = depthOf(step);
    while (stages.length <= d) stages.push([]);
    stages[d].push(step.key);
  }
  return stages.length ? stages : [[]];
}

/** The steps whose results `step` reads, by name. */
export function stepReads(step: DraftStep): string[] {
  return step.reads.filter((read) => !(INPUT_READS as readonly string[]).includes(read));
}

/**
 * Move a step up or down within its stage. Order across stages is untouched, because a stage is
 * not a place a step can be put (§4.3). Returns the same draft when the step is already at that end.
 */
export function moveStep(draft: WorkflowDraft, key: string, delta: -1 | 1): WorkflowDraft {
  const stage = stagesOf(draft.steps).find((keys) => keys.includes(key));
  if (!stage) return draft;
  const at = stage.indexOf(key);
  const neighbour = stage[at + delta];
  if (neighbour === undefined) return draft;
  const steps = [...draft.steps];
  const i = steps.findIndex((s) => s.key === key);
  const j = steps.findIndex((s) => s.key === neighbour);
  [steps[i], steps[j]] = [steps[j], steps[i]];
  return { ...draft, steps };
}

/**
 * A step name from a label, unique among `taken`. The same rule the server uses when it names a
 * step after an Agent (`step_name_for`); the name is an identifier in the JSON, not file syntax.
 */
export function stepNameFor(label: string, taken: readonly string[]): string {
  let base = label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 40)
    .replace(/_+$/g, '');
  if (!base || !/^[a-z]/.test(base)) base = base ? `step_${base}` : 'step';
  if ((INPUT_READS as readonly string[]).includes(base)) base = `${base}_step`;
  const used = new Set(taken);
  let candidate = base;
  let n = 2;
  while (used.has(candidate)) {
    candidate = `${base}_${n}`;
    n += 1;
  }
  return candidate;
}

/**
 * A new Agent step, appended to stage `stage` (0-based), or null at the cap. In stage 1 it reads the
 * request and the files; in a later stage it also reads every step of the stage before, which is
 * what puts it there (§4.1), and the author unticks what it should not read. `stage` may be one
 * past the last stage, which starts a new one.
 */
export function addStep(draft: WorkflowDraft, stage = 0): { draft: WorkflowDraft; key: string } | null {
  if (draft.steps.length >= MAX_WORKFLOW_STEPS) return null;
  const stages = stagesOf(draft.steps);
  const at = Math.max(0, Math.min(stage, stages.length));
  const byKey = new Map(draft.steps.map((s) => [s.key, s.name]));
  const before = at > 0 ? (stages[at - 1] ?? []).map((k) => byKey.get(k)!) : [];
  const key = nextKey();
  const step: DraftStep = {
    key,
    name: stepNameFor('step', draft.steps.map((s) => s.name)),
    autoName: true,
    agentId: null,
    agentVersion: null,
    reads: [...INPUT_READS, ...before],
    ownRequest: null,
  };
  return { draft: { ...draft, steps: [...draft.steps, step] }, key };
}

/**
 * The steps that read `key`'s result, directly or through other steps, each with the step it
 * reads on the way there (null when it reads `key` itself). Any of them is a read `key` can't take,
 * because it would make a cycle, which the format refuses (§4.1).
 */
export function readersOf(steps: readonly DraftStep[], key: string): Map<string, string | null> {
  const target = steps.find((s) => s.key === key);
  const found = new Map<string, string | null>();
  if (!target) return found;
  let frontier = [target.name];
  while (frontier.length) {
    const next: string[] = [];
    for (const name of frontier) {
      for (const step of steps) {
        if (step.key === key || found.has(step.key) || !step.reads.includes(name)) continue;
        // A direct reader reads the target itself; one further on, through the step it reads.
        found.set(step.key, name === target.name ? null : name);
        next.push(step.name);
      }
    }
    frontier = next;
  }
  return found;
}

/** The steps the Result takes, by name, in step order: every step while `reads` is null. */
export function resultSteps(draft: WorkflowDraft): string[] {
  const reads = draft.result.reads;
  const names = draft.steps.map((s) => s.name);
  return reads === null ? names : names.filter((n) => reads.includes(n));
}

/**
 * Include or leave out one step's result. Every step ticked is stored as null, which the format
 * writes as no `Reads:` line (the result takes every step, steps added later included). The last
 * one can't be left out: the format refuses a result that reads no step.
 */
export function toggleResultRead(draft: WorkflowDraft, name: string): WorkflowDraft {
  const current = resultSteps(draft);
  const next = current.includes(name) ? current.filter((n) => n !== name) : [...current, name];
  if (!next.length) return draft;
  const ordered = draft.steps.map((s) => s.name).filter((n) => next.includes(n));
  const reads = ordered.length === draft.steps.length ? null : ordered;
  return { ...draft, result: { ...draft.result, reads } };
}

export function setResult(
  draft: WorkflowDraft,
  patch: Partial<Pick<WorkflowDraft['result'], 'mode' | 'instruction'>>,
): WorkflowDraft {
  return { ...draft, result: { ...draft.result, ...patch } };
}

/** Remove a step, and every read of it (other steps' and the result's). */
export function removeStep(draft: WorkflowDraft, key: string): WorkflowDraft {
  const gone = draft.steps.find((s) => s.key === key);
  if (!gone) return draft;
  const steps = draft.steps
    .filter((s) => s.key !== key)
    .map((s) => ({ ...s, reads: s.reads.filter((r) => r !== gone.name) }));
  // A result left reading nothing takes every step again: the format refuses one that reads none.
  const left = draft.result.reads?.filter((r) => r !== gone.name) ?? null;
  const reads = left && left.length ? left : null;
  return { ...draft, steps, result: { ...draft.result, reads } };
}

/**
 * Give a step its Agent. The version it recorded belongs to the old Agent, so it is cleared and
 * the save records the new one's. A step still on its placeholder name takes the Agent's, and
 * every read of the old name follows it.
 */
export function chooseAgent(draft: WorkflowDraft, key: string, agentId: string, agentName: string): WorkflowDraft {
  const step = draft.steps.find((s) => s.key === key);
  if (!step || step.agentId === agentId) return draft;
  const taken = draft.steps.filter((s) => s.key !== key).map((s) => s.name);
  const name = step.autoName ? stepNameFor(agentName, taken) : step.name;
  const rename = (read: string) => (read === step.name ? name : read);
  const steps = draft.steps.map((s) =>
    s.key === key
      ? { ...s, name, agentId, agentVersion: null }
      : { ...s, reads: s.reads.map(rename) },
  );
  const reads = draft.result.reads?.map(rename) ?? null;
  return { ...draft, steps, result: { ...draft.result, reads } };
}

export function updateStep(draft: WorkflowDraft, key: string, patch: Partial<Pick<DraftStep, 'reads' | 'ownRequest'>>): WorkflowDraft {
  return { ...draft, steps: draft.steps.map((s) => (s.key === key ? { ...s, ...patch } : s)) };
}

/** Each Agent the server last described, by id, from a document. */
export function agentViews(document: WorkflowDocument | null): Map<string, WorkflowAgentView> {
  const map = new Map<string, WorkflowAgentView>();
  for (const step of document?.workflow.steps ?? []) map.set(step.agentId, step.agent);
  return map;
}
