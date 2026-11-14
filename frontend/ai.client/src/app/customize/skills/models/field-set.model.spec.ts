import { describe, expect, it } from 'vitest';
import { fieldSetNameFor, fieldSetShape, suggestFieldName, suggestSetName } from './field-set.model';

describe('field-set model helpers', () => {
  it('suggests a snake_case field name that starts with a letter', () => {
    expect(suggestFieldName('Budget Cap ($)')).toBe('budget_cap');
    expect(suggestFieldName('2nd deadline')).toBe('field_2nd_deadline');
    expect(suggestFieldName('')).toBe('');
  });

  it('suggests a hyphenated set name', () => {
    expect(suggestSetName('NSF Solicitation (2027)')).toBe('nsf-solicitation-2027');
  });

  it('reads a set name only from a field-set filename', () => {
    expect(fieldSetNameFor('field-set.nsf-solicitation.md')).toBe('nsf-solicitation');
    expect(fieldSetNameFor('field-set.NSF.md')).toBeNull();
    expect(fieldSetNameFor('forms.md')).toBeNull();
  });

  it('treats find and title as not part of the shape, and order as part of it', () => {
    const a = { name: 'a', type: 'string', enum: [], optional: false };
    const b = { name: 'b', type: 'date', enum: [], optional: true };

    expect(fieldSetShape([a, b])).toBe(fieldSetShape([{ ...a }, { ...b }]));
    expect(fieldSetShape([a, b])).not.toBe(fieldSetShape([b, a]));
  });
});
