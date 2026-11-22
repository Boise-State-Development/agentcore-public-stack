import { describe, expect, it } from 'vitest';

import {
  DraftStep,
  PROPOSAL_REVIEW_TEMPLATE,
  WorkflowDraft,
  addStep,
  agentViews,
  blankDraft,
  chooseAgent,
  choosePromptModel,
  draftFrom,
  fieldSetViews,
  isComplete,
  MAX_CHECKLIST_FIELD_SETS,
  moveFieldSet,
  toggleFieldSet,
  modelViews,
  renameStep,
  setStepKind,
  moveStep,
  readersOf,
  removeStep,
  resultSteps,
  sameDraft,
  setResult,
  stagesOf,
  stepNameFor,
  templateDraft,
  toWrite,
  toggleResultRead,
} from './workflow-draft';
import { MAX_WORKFLOW_STEPS, WorkflowDocument, workflowNameFor } from './workflow.models';

const VIEW = { canRun: true, hasIcon: false, changed: false, runsDraft: false };

function step(key: string, name: string, overrides: Partial<DraftStep> = {}): DraftStep {
  return { key, name, autoName: false, kind: 'agent', agentId: `AST-${name}`, agentVersion: null, modelId: null, reads: ['request', 'files'], ownRequest: null, fieldSets: [], ...overrides };
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

  it('gives the result every step again when it read only the removed one, since none is refused', () => {
    const d = draft([step('a', 'one'), step('b', 'two')]);
    const next = removeStep({ ...d, result: { ...d.result, reads: ['one'] } }, 'a');
    expect(next.result.reads).toBeNull();
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

  it('adds to stage 1 reading the request and the files', () => {
    const added = addStep(draft([step('a', 'one')]), 0)!;
    expect(added.draft.steps.at(-1)!.reads).toEqual(['request', 'files']);
    expect(stagesOf(added.draft.steps)).toEqual([['a', added.key]]);
  });

  it('adds to a later stage reading every step of the stage before, which is what puts it there', () => {
    const d = draft([step('a', 'one'), step('b', 'two'), step('c', 'three', { reads: ['one'] })]);
    const second = addStep(d, 1)!;
    expect(second.draft.steps.at(-1)!.reads).toEqual(['request', 'files', 'one', 'two']);
    expect(stagesOf(second.draft.steps)).toEqual([['a', 'b'], ['c', second.key]]);

    // One past the last stage starts a new one, reading the last stage.
    const third = addStep(d, 2)!;
    expect(third.draft.steps.at(-1)!.reads).toEqual(['request', 'files', 'three']);
    expect(stagesOf(third.draft.steps)).toEqual([['a', 'b'], ['c'], [third.key]]);
  });
});

describe('readersOf', () => {
  it('names every step that reads a step, directly or on the way, so none of them can be read back', () => {
    const steps = [
      step('a', 'one'),
      step('b', 'two', { reads: ['one'] }),
      step('c', 'three', { reads: ['two'] }),
      step('d', 'four'),
    ];
    const readers = readersOf(steps, 'a');
    expect([...readers.entries()]).toEqual([
      ['b', null],
      ['c', 'two'],
    ]);
    expect(readersOf(steps, 'd').size).toBe(0);
    // A step is never its own reader: the tab never lists it at all.
    expect(readersOf(steps, 'c').size).toBe(0);
  });
});

describe('the result', () => {
  it('takes every step while it names none, and only what it names otherwise, in step order', () => {
    const d = draft([step('a', 'one'), step('b', 'two'), step('c', 'three')]);
    expect(resultSteps(d)).toEqual(['one', 'two', 'three']);
    expect(resultSteps({ ...d, result: { ...d.result, reads: ['three', 'one'] } })).toEqual(['one', 'three']);
  });

  it('leaves a step out, keeps at least one, and goes back to every step when all are ticked', () => {
    let d = draft([step('a', 'one'), step('b', 'two')]);
    d = toggleResultRead(d, 'one');
    expect(d.result.reads).toEqual(['two']);
    expect(toggleResultRead(d, 'two')).toBe(d);
    d = toggleResultRead(d, 'one');
    expect(d.result.reads).toBeNull();
  });

  it('sends the merge, its instruction and its reads, and a blank instruction as none', () => {
    const d = setResult(draft([step('a', 'one'), step('b', 'two')]), { mode: 'merge', instruction: 'Write one report.' });
    expect(toWrite({ ...d, result: { ...d.result, reads: ['two'] } })!.result).toEqual({
      mode: 'merge',
      reads: ['two'],
      instruction: 'Write one report.',
    });
    expect(toWrite(setResult(d, { instruction: '   ' }))!.result.instruction).toBeNull();
    expect(sameDraft(setResult(d, { instruction: '   ' }), setResult(d, { instruction: null }))).toBe(true);
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

describe('prompt steps (workflow-designer §21.3, PR-D.6c)', () => {
  const prompt = (key: string, name: string, overrides: Partial<DraftStep> = {}): DraftStep =>
    step(key, name, { kind: 'prompt', agentId: null, modelId: 'us.anthropic.claude-haiku-5-5', ...overrides });

  it('writes a prompt step with its model as its worker and no Agent', () => {
    const body = toWrite(draft([step('a', 'generalist'), prompt('b', 'budget', { reads: ['files'], ownRequest: 'List the budget risks' })]));
    expect(body?.steps[1]).toEqual({
      name: 'budget',
      kind: 'prompt',
      agentId: '',
      modelId: 'us.anthropic.claude-haiku-5-5',
      reads: ['files'],
      request: 'List the budget risks',
    });
    expect('agentVersion' in (body?.steps[1] ?? {})).toBe(false);
  });

  it('is complete only once a prompt step has a model', () => {
    const d = draft([step('a', 'generalist'), prompt('b', 'budget', { modelId: null })]);
    expect(isComplete(d)).toBe(false);
    expect(toWrite(d)).toBeNull();
    expect(isComplete(choosePromptModel(d, 'b', 'm'))).toBe(true);
  });

  it('compares kind and model in the dirty check', () => {
    const base = draft([prompt('b', 'budget')]);
    expect(sameDraft(base, draft([prompt('b', 'budget')]))).toBe(true);
    expect(sameDraft(base, draft([prompt('b', 'budget', { modelId: 'other' })]))).toBe(false);
    expect(sameDraft(base, draft([step('b', 'budget')]))).toBe(false);
  });

  it('switches a step’s kind, clearing the old worker and keeping its reads and request', () => {
    const d = draft([step('a', 'generalist', { ownRequest: 'Read it', reads: ['request'] })]);
    const asPrompt = setStepKind(d, 'a', 'prompt');
    expect(asPrompt.steps[0]).toMatchObject({ kind: 'prompt', agentId: null, modelId: null, reads: ['request'], ownRequest: 'Read it' });
    const back = setStepKind(choosePromptModel(asPrompt, 'a', 'm'), 'a', 'agent');
    expect(back.steps[0]).toMatchObject({ kind: 'agent', agentId: null, modelId: null });
  });

  it('a new step on its placeholder name takes the prompt placeholder, and readers follow a rename', () => {
    const added = addStep(draft([step('a', 'generalist')]), 1)!;
    const asPrompt = setStepKind(added.draft, added.key, 'prompt');
    const promptStep = asPrompt.steps.find((s) => s.key === added.key)!;
    expect(promptStep.name).toBe('prompt');
    expect(promptStep.autoName).toBe(true);

    const withReader = { ...asPrompt, steps: [...asPrompt.steps, step('c', 'editor', { reads: ['prompt'] })], result: { mode: 'card' as const, reads: ['prompt'], instruction: null } };
    const renamed = renameStep(withReader, added.key, 'Budget check');
    expect(renamed.steps.find((s) => s.key === added.key)).toMatchObject({ name: 'budget_check', autoName: false });
    expect(renamed.steps.find((s) => s.key === 'c')?.reads).toEqual(['budget_check']);
    expect(renamed.result.reads).toEqual(['budget_check']);
    expect(renameStep(renamed, added.key, '   ')).toBe(renamed);
  });

  it('an Agent can’t be chosen for a prompt step', () => {
    const d = draft([prompt('b', 'budget')]);
    expect(chooseAgent(d, 'b', 'AST-x', 'X')).toBe(d);
  });

  it('reads a saved prompt step back with the model its file names', () => {
    const fresh = draftFrom({
      skillId: 's',
      name: 'w',
      title: 'W',
      version: 2,
      files: 'any',
      steps: [
        {
          name: 'budget',
          kind: 'prompt',
          agentId: '',
          modelId: 'us.anthropic.claude-haiku-5-5',
          reads: ['request', 'files'],
          effectiveRequest: 'r',
          agent: { ...VIEW, modelId: 'us.anthropic.claude-haiku-5-6' },
          model: { canRun: true, modelId: 'us.anthropic.claude-haiku-5-6', requestedModelId: 'us.anthropic.claude-haiku-5-5' },
        },
      ],
      result: { mode: 'card', steps: ['budget'] },
      fingerprint: 'f',
      isOwned: true,
    });
    expect(fresh.steps[0]).toMatchObject({ kind: 'prompt', agentId: null, modelId: 'us.anthropic.claude-haiku-5-5' });
  });

  it('keeps prompt steps out of the Agent views and gives their models by step name', () => {
    const document = {
      workflow: {
        steps: [
          { name: 'budget', kind: 'prompt', agentId: '', reads: [], effectiveRequest: '', agent: VIEW, model: { canRun: false, modelId: 'm', reason: 'no_access' as const } },
          { name: 'generalist', kind: 'agent', agentId: 'AST-1', reads: [], effectiveRequest: '', agent: VIEW },
        ],
      },
    } as unknown as WorkflowDocument;
    expect([...agentViews(document).keys()]).toEqual(['AST-1']);
    expect(modelViews(document).get('budget')?.reason).toBe('no_access');
  });
});

describe('checklist steps (PR-D.7c)', () => {
  const checklist = (overrides: Partial<DraftStep> = {}): DraftStep =>
    step('k1', 'audit', { kind: 'checklist', agentId: null, modelId: 'us.m', reads: ['files'], fieldSets: ['budget'], ...overrides });

  it('becomes a checklist step keeping a prompt step’s model, and reads the files it must', () => {
    const prompt = step('k1', 'budget_check', { kind: 'prompt', agentId: null, modelId: 'us.m', reads: ['request'] });
    const next = setStepKind(draft([prompt]), 'k1', 'checklist').steps[0];
    expect(next).toMatchObject({ kind: 'checklist', modelId: 'us.m', reads: ['request', 'files'], fieldSets: [] });
    // From an Agent step, nothing carries over: the model is chosen afresh.
    expect(setStepKind(draft([step('k1', 'generalist')]), 'k1', 'checklist').steps[0]).toMatchObject({ agentId: null, modelId: null });
    // Back to an Agent step, its sets go.
    expect(setStepKind(draft([checklist()]), 'k1', 'agent').steps[0].fieldSets).toEqual([]);
    // A placeholder name takes the kind's.
    const blank = addStep(blankDraft())!;
    expect(setStepKind(blank.draft, blank.key, 'checklist').steps[0].name).toBe('checklist');
  });

  it('is complete with a model, a field set and the files, and not before', () => {
    expect(isComplete(draft([checklist()]))).toBe(true);
    expect(isComplete(draft([checklist({ fieldSets: [] })]))).toBe(false);
    expect(isComplete(draft([checklist({ modelId: null })]))).toBe(false);
    expect(isComplete(draft([checklist({ reads: ['request'] })]))).toBe(false);
  });

  it('adds sets at the end, never twice or past the cap, and moves them within the step', () => {
    let d = toggleFieldSet(draft([checklist()]), 'k1', 'compliance');
    expect(d.steps[0].fieldSets).toEqual(['budget', 'compliance']);
    d = moveFieldSet(d, 'k1', 'compliance', -1);
    expect(d.steps[0].fieldSets).toEqual(['compliance', 'budget']);
    expect(moveFieldSet(d, 'k1', 'compliance', -1)).toBe(d);
    d = toggleFieldSet(d, 'k1', 'compliance');
    expect(d.steps[0].fieldSets).toEqual(['budget']);
    const full = draft([checklist({ fieldSets: Array.from({ length: MAX_CHECKLIST_FIELD_SETS }, (_, i) => `set-${i}`) })]);
    expect(toggleFieldSet(full, 'k1', 'one-more')).toBe(full);
    // Only a checklist step has sets.
    const agent = draft([step('k1', 'generalist')]);
    expect(toggleFieldSet(agent, 'k1', 'budget')).toBe(agent);
  });

  it('writes its model, sets in order and guidance, and reads them back', () => {
    const d = draft([checklist({ fieldSets: ['compliance', 'budget'], ownRequest: 'Treat a gap as a fail.' })]);
    expect(toWrite(d)!.steps[0]).toEqual({
      name: 'audit',
      kind: 'checklist',
      agentId: '',
      modelId: 'us.m',
      reads: ['files'],
      request: 'Treat a gap as a fail.',
      fieldSets: ['compliance', 'budget'],
    });
    const back = draftFrom({
      skillId: 'S',
      name: 'w',
      title: 'W',
      version: 1,
      files: 'any',
      steps: [
        {
          name: 'audit',
          kind: 'checklist',
          agentId: '',
          modelId: 'us.m',
          reads: ['files'],
          request: 'Treat a gap as a fail.',
          effectiveRequest: 'Treat a gap as a fail.',
          agent: { canRun: true, hasIcon: false, changed: false, runsDraft: false },
          model: { canRun: true, modelId: 'us.m-next', requestedModelId: 'us.m' },
          fieldSets: [{ name: 'compliance', canRun: true }, { name: 'budget', canRun: false, reason: 'missing' }],
        },
      ],
      result: { mode: 'card', steps: ['audit'] },
      fingerprint: 'f',
      isOwned: true,
    });
    expect(back.steps[0]).toMatchObject({ kind: 'checklist', agentId: null, modelId: 'us.m', fieldSets: ['compliance', 'budget'] });
  });

  it('is dirty when its sets change order: the order is what it runs', () => {
    const a = draft([checklist({ fieldSets: ['budget', 'compliance'] })]);
    expect(sameDraft(a, moveFieldSet(a, 'k1', 'compliance', -1))).toBe(false);
  });

  it('keeps each set the server read by name, and no Agent for the step', () => {
    const document = {
      workflow: {
        steps: [
          {
            name: 'audit',
            kind: 'checklist',
            agentId: '',
            agent: { canRun: true },
            model: { canRun: true, modelId: 'us.m' },
            fieldSets: [{ name: 'budget', canRun: true, title: 'Budget', fieldCount: 10 }],
          },
        ],
      },
    } as unknown as WorkflowDocument;
    expect(fieldSetViews(document).get('budget')?.fieldCount).toBe(10);
    expect(agentViews(document).size).toBe(0);
    expect(modelViews(document).get('audit')?.modelId).toBe('us.m');
  });
});
