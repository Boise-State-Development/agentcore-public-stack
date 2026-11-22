// Checklist steps on the SPA (docs/specs/workflow-designer.md §22, PR-D.7c): the pure rules the
// sheet, the run row and the sent bubble share. The components' rendering is pinned in their specs.
import { describe, expect, it } from 'vitest';

import { checklistTable } from '../../components/handoff/workflow-run-row.component';
import { promptStepLabel } from '../../components/handoff/workflow-run-sheet.component';
import { groupRunResults } from './handoff-prompt';
import { DeliveryBlock } from './handoff.models';
import { checklistEstimate, estimateRunInputUsd } from './workflow-run-estimate';
import {
  checklistSetErrorLine,
  fieldSetsLabel,
  isModelRunStep,
  promptWorkerLabel,
  stepsLabel,
  workerName,
} from './workflow-run-progress';
import { canStart, stepBlockerCopy } from './workflow-run.service';
import { RunReview } from './workflow-run.models';

describe('checklist step labels', () => {
  it('name a checklist step "{Step} · Checklist · {model}", and count its sets', () => {
    expect(promptWorkerLabel('Haiku 5.5', 'checklist')).toBe('Checklist · Haiku 5.5');
    expect(promptWorkerLabel('Haiku 5.5')).toBe('Prompt · Haiku 5.5');
    expect(fieldSetsLabel(1)).toBe('1 field set');
    expect(fieldSetsLabel(4)).toBe('4 field sets');
    expect(promptStepLabel(undefined, 'Haiku 5.5', 'checklist')).toBe('Checklist · Haiku 5.5');
    expect(promptStepLabel('compliance_audit', 'Haiku 5.5', 'checklist')).toBe('Compliance audit');
    expect(workerName({ kind: 'checklist', agentName: 'Haiku 5.5', stepName: undefined })).toBe('Checklist · Haiku 5.5');
    expect(isModelRunStep({ kind: 'checklist' })).toBe(true);
    expect(isModelRunStep({ kind: 'agent' })).toBe(false);
    // A run with a checklist step is "steps", since it has no Agent.
    expect(stepsLabel(6, true)).toBe('6 steps');
  });

  it('say why a set failed in plain words, never the code', () => {
    for (const code of ['field_set_missing', 'unsupported_field', 'no_tool_call', 'timeout', 'cut_off', 'model_error', 'something_new', null]) {
      const line = checklistSetErrorLine(code);
      expect(line).not.toMatch(/_/);
      expect(line.endsWith('.')).toBe(true);
    }
    expect(checklistSetErrorLine('timeout')).toBe('It took too long, so it was stopped.');
  });
});

describe('the checklist estimate', () => {
  const doc = 10_000;

  it('counts the documents once per model, then at the cache-read rate, one call per set', () => {
    const checklist = (fieldCounts: number[], model = 'us.anthropic.claude-haiku-5-5') => ({
      modelId: model,
      inputPricePerMillionTokens: 1,
      request: '',
      ...checklistEstimate(model, fieldCounts.map((fieldCount) => ({ fieldCount }))),
    });
    const one = estimateRunInputUsd({ steps: [checklist([5])], card: 'card text', fileTokens: [doc] })!;
    const four = estimateRunInputUsd({ steps: [checklist([5, 5, 5, 5])], card: 'card text', fileTokens: [doc] })!;
    // No card; one full read of the documents, three cache reads, and four field lists with the frame.
    const perCall = 850 + 5 * 40;
    expect(one).toBeCloseTo(((doc + perCall) * 1.4) / 1_000_000, 10);
    expect(four).toBeCloseTo(((doc + 3 * doc * 0.1 + 4 * perCall) * 1.4) / 1_000_000, 10);
    // Two checklist steps on one model share the one write; on another model they don't.
    const sameModel = estimateRunInputUsd({ steps: [checklist([5]), checklist([5])], card: '', fileTokens: [doc] })!;
    const otherModel = estimateRunInputUsd({ steps: [checklist([5]), checklist([5], 'us.other')], card: '', fileTokens: [doc] })!;
    expect(sameModel).toBeLessThan(otherModel);
    expect(otherModel).toBeCloseTo(2 * one, 10);
  });

  it('is unavailable until every file is sized, as for a prompt step', () => {
    const steps = [{ modelId: 'm', inputPricePerMillionTokens: 1, request: '', ...checklistEstimate('m', [{ fieldCount: 3 }]) }];
    expect(estimateRunInputUsd({ steps, card: '', fileTokens: [null] })).toBeNull();
  });
});

describe('a checklist step on the sheet', () => {
  function review(ownRequest: string | null, kind?: 'checklist'): RunReview {
    return {
      draft: { agents: [], request: '' },
      phase: 'ready',
      request: '',
      steps: [{ stepId: 's1', handoffId: 'h1', agentId: '', agentName: 'Haiku 5.5', ownRequest, removed: false, ...(kind ? { kind } : {}) }],
      card: '',
      cardRemoved: false,
      fileIds: [],
    };
  }

  it('starts with no guidance: its sets are what it is asked', () => {
    expect(canStart(review('', 'checklist'))).toBe(true);
    // Any other step still needs a request.
    expect(canStart(review(''))).toBe(false);
  });

  it('says its blockers in the sheet’s words', () => {
    expect(stepBlockerCopy({ code: 'checklist_steps_off', message: 'x' })).toBe('Checklist steps aren’t available here yet. Remove the step to start.');
    expect(
      stepBlockerCopy({ code: 'field_set_unavailable', message: "This step's field set 'budget' has a list field, which a checklist step can't fill yet. Remove the step, or change the field's type" }),
    ).toBe("This step's field set 'budget' has a list field, which a checklist step can't fill yet. Remove the step, or change the field's type.");
  });
});

describe('checklistTable', () => {
  it('reads a set row as one extraction table, headed by the set, naming no document', () => {
    const table = checklistTable({
      runId: 'run1',
      stepId: 's3',
      fieldSet: 'budget',
      setIndex: 0,
      fieldSetTitle: 'Budget values',
      modelId: 'us.m',
      status: 'done',
      quotesOmitted: true,
      fields: [
        { name: 'cap', title: 'Cap', type: 'currency', value: '$600,000', state: 'supported', method: 'numeric', quote: 'up to $600,000', locator: { kind: 'page', value: 2, precision: 'measured' } },
        { name: 'trips', title: 'Trips', type: 'number', value: '2', state: 'supported', method: 'number_word', quote: 'Two domestic conferences' },
        { name: 'equipment', title: 'Equipment', type: 'currency', value: null, state: 'unverified', method: 'no_quote' },
      ],
    });
    expect(table.title).toBe('Budget values');
    expect(table.error).toBeUndefined();
    const run = table.runs![0];
    expect(run.filename).toBe('');
    expect(run.schemaEnforced).toBe(true);
    expect(run.quotesOmitted).toBe(true);
    expect(run.fields.map((f) => [f.name, f.value, f.state])).toEqual([
      ['cap', '$600,000', 'supported'],
      ['trips', '2', 'supported'],
      // A value not found is empty, which the table says as "Not found".
      ['equipment', '', 'unverified'],
    ]);
    expect(run.fields[0].locator).toEqual({ kind: 'page', value: 2, precision: 'measured' });
  });

  it('gives a failed set no table, and says why', () => {
    const table = checklistTable({ runId: 'r', stepId: 's', fieldSet: 'compliance', setIndex: 1, status: 'error', error: 'no_tool_call', fields: [] });
    expect(table).toEqual({
      key: 's:compliance',
      title: 'compliance',
      runs: null,
      error: 'The model answered without filling in the fields, twice.',
    });
  });
});

describe('a sent run result with a checklist part', () => {
  const block = (fromTitle: string, text = 'answer'): DeliveryBlock => ({ fromTitle, text, artifacts: [], files: [] });

  it('groups a checklist part by its "{model} · {step}" label, and tags it as a checklist step', () => {
    const groups = groupRunResults(
      [block('Generalist · Proposal review'), block('Haiku 5.5 · compliance_audit', '### Compliance')],
      [{ runId: 'run1', agentNames: ['Generalist', 'Haiku 5.5'], promptSteps: [null, 'compliance_audit'], checklistSteps: [false, true] }],
    );
    expect(groups).toHaveLength(1);
    const parts = groups[0].kind === 'run' ? groups[0].parts : [];
    expect(parts.map((part) => [part.agentName, part.promptStep, part.checklist])).toEqual([
      ['Generalist', undefined, undefined],
      ['Haiku 5.5', 'compliance_audit', true],
    ]);
  });

  it('matches a nameless checklist part on "Checklist step", not the prompt step’s fallback', () => {
    const runs = [{ runId: 'run1', agentNames: ['Generalist', 'Haiku 5.5'], promptSteps: [null, ''], checklistSteps: [false, true] }];
    expect(groupRunResults([block('Generalist'), block('Haiku 5.5 · Checklist step')], runs)[0].kind).toBe('run');
    expect(groupRunResults([block('Generalist'), block('Haiku 5.5 · Prompt step')], runs).map((g) => g.kind)).toEqual(['card', 'card']);
  });
});
