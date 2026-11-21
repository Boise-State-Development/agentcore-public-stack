import { describe, expect, it } from 'vitest';

import {
  DraftStep,
  PROPOSAL_REVIEW_TEMPLATE,
  WorkflowDraft,
  addStep,
  blankDraft,
  chooseAgent,
  moveStep,
  removeStep,
  sameDraft,
  stagesOf,
  stepNameFor,
  templateDraft,
  toWrite,
} from './workflow-draft';
import { MAX_WORKFLOW_STEPS, workflowNameFor } from './workflow.models';

const VIEW = { canRun: true, hasIcon: false, changed: false, runsDraft: false };

function step(key: string, name: string, overrides: Partial<DraftStep> = {}): DraftStep {
  return { key, name, autoName: false, agentId: `AST-${name}`, agentVersion: null, reads: ['request', 'files'], ownRequest: null, ...overrides };
}

function draft(steps: DraftStep[]): WorkflowDraft {
  return { ...blankDraft('Review'), steps };
}

describe('stagesOf', () => {
  it('puts a step that reads no other step in stage 1, in authored order', () => {
    expect(stagesOf([step('a', 'one'), step('b', 'two')])).toEqual([['a', 'b']]);
  });

  it('puts a reader in the stage after the latest step it reads', () => {
    const steps = [
      step('a', 'one'),
      step('b', 'two', { reads: ['one'] }),
      step('c', 'three', { reads: ['request', 'one', 'two'] }),
      step('d', 'four'),
    ];
    expect(stagesOf(steps)).toEqual([['a', 'd'], ['b'], ['c']]);
  });

  it('is one empty stage for a workflow with no steps, so Add step has a column', () => {
    expect(stagesOf([])).toEqual([[]]);
  });

  it('reads a cycle as no edge rather than looping', () => {
    const steps = [step('a', 'one', { reads: ['two'] }), step('b', 'two', { reads: ['one'] })];
    expect(stagesOf(steps).flat().sort()).toEqual(['a', 'b']);
  });
});

describe('moveStep', () => {
  it('swaps a step with its neighbour in the same stage', () => {
    const d = draft([step('a', 'one'), step('b', 'two'), step('c', 'three')]);
    expect(moveStep(d, 'b', -1).steps.map((s) => s.key)).toEqual(['b', 'a', 'c']);
    expect(moveStep(d, 'b', 1).steps.map((s) => s.key)).toEqual(['a', 'c', 'b']);
  });

  it('returns the same draft at an end', () => {
    const d = draft([step('a', 'one'), step('b', 'two')]);
    expect(moveStep(d, 'a', -1)).toBe(d);
    expect(moveStep(d, 'b', 1)).toBe(d);
  });

  it('never moves a step across a stage', () => {
    const d = draft([step('a', 'one'), step('b', 'two', { reads: ['one'] }), step('c', 'three')]);
    // b is alone in stage 2, so it has nowhere to go.
    expect(moveStep(d, 'b', -1)).toBe(d);
    // c's neighbour in stage 1 is a, two places up in the list.
    expect(moveStep(d, 'c', -1).steps.map((s) => s.key)).toEqual(['c', 'b', 'a']);
  });
});

describe('the template', () => {
  it('is four reviewers and a card, with no Agent on any step', () => {
    const t = templateDraft(PROPOSAL_REVIEW_TEMPLATE)!;
    expect(t.steps).toHaveLength(4);
    expect(t.steps.every((s) => s.agentId === null)).toBe(true);
    expect(t.result.mode).toBe('card');
    expect(toWrite(t)).toBeNull();
  });

  it('is null for a template that does not exist', () => {
    expect(templateDraft('nope')).toBeNull();
  });
});

describe('choosing an Agent', () => {
  it('renames a just-added step after its Agent, and reads of it follow', () => {
    const added = addStep(draft([step('a', 'one')]))!;
    let d = added.draft;
    d = { ...d, steps: d.steps.map((s) => (s.key === 'a' ? { ...s, reads: ['request', 'step'] } : s)), result: { ...d.result, reads: ['step'] } };

    d = chooseAgent(d, added.key, 'AST-fis', 'Fiscal reader');

    const chosen = d.steps.find((s) => s.key === added.key)!;
    expect(chosen.name).toBe('fiscal_reader');
    expect(chosen.agentId).toBe('AST-fis');
    expect(d.steps[0].reads).toEqual(['request', 'fiscal_reader']);
    expect(d.result.reads).toEqual(['fiscal_reader']);
  });

  it('keeps a template step’s name and clears the version it recorded for another Agent', () => {
    const d = draft([step('a', 'generalist', { agentVersion: 3 })]);
    const next = chooseAgent(d, 'a', 'AST-new', 'Some Agent');
    expect(next.steps[0].name).toBe('generalist');
    expect(next.steps[0].agentVersion).toBeNull();
  });
});

describe('removeStep', () => {
  it('drops every read of the removed step', () => {
    const d = draft([step('a', 'one'), step('b', 'two', { reads: ['request', 'one'] })]);
    const next = removeStep({ ...d, result: { ...d.result, reads: ['one', 'two'] } }, 'a');
    expect(next.steps.map((s) => s.key)).toEqual(['b']);
    expect(next.steps[0].reads).toEqual(['request']);
    expect(next.result.reads).toEqual(['two']);
  });
});

describe('addStep', () => {
  it('stops at the cap', () => {
    let d = blankDraft();
    for (let i = 0; i < MAX_WORKFLOW_STEPS; i++) d = addStep(d)!.draft;
    expect(d.steps).toHaveLength(MAX_WORKFLOW_STEPS);
    expect(addStep(d)).toBeNull();
    expect(new Set(d.steps.map((s) => s.name)).size).toBe(MAX_WORKFLOW_STEPS);
  });
});

describe('toWrite', () => {
  it('records the Agent’s published version for a step that has none', () => {
    const d = draft([step('a', 'one'), step('b', 'two', { agentVersion: 2 })]);
    const body = toWrite(d, new Map([['AST-one', { ...VIEW, publishedVersion: 5 }], ['AST-two', { ...VIEW, publishedVersion: 9 }]]), 4)!;
    expect(body.steps.map((s) => s.agentVersion)).toEqual([5, 2]);
    expect(body.expectedVersion).toBe(4);
  });

  it('sends a blank own request as the run’s', () => {
    const body = toWrite(draft([step('a', 'one', { ownRequest: '  ' })]))!;
    expect(body.steps[0].request).toBeNull();
  });
});

describe('sameDraft', () => {
  it('ignores keys and read order, and sees any real change', () => {
    const a = draft([step('a', 'one')]);
    const b = draft([step('z', 'one', { reads: ['files', 'request'] })]);
    expect(sameDraft(a, b)).toBe(true);
    expect(sameDraft(a, { ...b, request: 'Something else' })).toBe(false);
  });
});

describe('names', () => {
  it('names a step the way the server does', () => {
    expect(stepNameFor('Fiscal Reader', [])).toBe('fiscal_reader');
    expect(stepNameFor('Fiscal Reader', ['fiscal_reader'])).toBe('fiscal_reader_2');
    expect(stepNameFor('42 things', [])).toBe('step_42_things');
    expect(stepNameFor('Request', [])).toBe('request_step');
    expect(stepNameFor('!!!', [])).toBe('step');
  });

  it('reads a workflow name from a carrying skill’s filename only', () => {
    expect(workflowNameFor('workflow.proposal-review.md')).toBe('proposal-review');
    expect(workflowNameFor('field-set.intake.md')).toBeNull();
    expect(workflowNameFor('workflow.Bad_Name.md')).toBeNull();
  });
});
