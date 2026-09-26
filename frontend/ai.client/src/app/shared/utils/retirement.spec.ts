import { describe, it, expect } from 'vitest';
import { isRetiring, modelRetirementDetail } from './retirement';

/**
 * The model retirement sentence (docs/specs/model-retirement.md §7). A model
 * differs from a tool in one way that changes it: it usually has a successor
 * that runs in its place, so "it stops working" is only true when it has none.
 * The tool copy (`retirementDetail`) keeps its own spec in services/tool.
 */
describe('isRetiring', () => {
  it.each([
    [{ status: 'deprecated' }, true],
    [{ status: 'retired' }, true],
    [{ status: 'active' }, false],
    [{ status: null }, false],
    [{}, false],
  ])('%j → %s', (item, expected) => {
    expect(isRetiring(item)).toBe(expected);
  });
});

describe('modelRetirementDetail', () => {
  const date = '2026-10-31';

  it('names the successor and the date while deprecated', () => {
    const text = modelRetirementDetail({ status: 'deprecated', successorName: 'Sonnet 5', retiresOn: date });
    expect(text).toMatch(/^From October 31, 2026, Sonnet 5 answers in its place\.$/);
  });

  it('names the successor alone when no date is recorded', () => {
    expect(modelRetirementDetail({ status: 'deprecated', successorName: 'Sonnet 5' })).toBe(
      'Sonnet 5 will answer in its place.',
    );
  });

  it('says it stops working only when there is no successor', () => {
    expect(modelRetirementDetail({ status: 'deprecated', retiresOn: date })).toBe(
      'It stops working on October 31, 2026.',
    );
  });

  it('speaks in the present once retired', () => {
    expect(modelRetirementDetail({ status: 'retired', successorName: 'Sonnet 5', retiresOn: date })).toBe(
      'Sonnet 5 now answers in its place.',
    );
  });

  it('leads with the admin note, without doubling its full stop', () => {
    expect(
      modelRetirementDetail({ status: 'deprecated', successorName: 'Sonnet 5', retirementNote: 'Faster and cheaper.' }),
    ).toBe('Faster and cheaper. Sonnet 5 will answer in its place.');
  });

  it('says nothing when nothing is recorded', () => {
    expect(modelRetirementDetail({ status: 'deprecated' })).toBe('');
    expect(modelRetirementDetail({ status: 'retired' })).toBe('');
  });
});
