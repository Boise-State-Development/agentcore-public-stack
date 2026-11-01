import { describe, expect, it } from 'vitest';
import {
  age,
  cleanFundingValues,
  describeExtensionAmount,
  describeFundingSource,
  fundingFieldError,
  standingConfirmation,
} from './quota-admin-copy';

const FIELDS = [
  { key: 'fund', label: 'Fund', required: true, pattern: '\\d{6}' },
  { key: 'org', label: 'Org', required: false },
];

describe('quota admin copy', () => {
  it('describes a fund by the schema labels in schema order, with its name first', () => {
    expect(describeFundingSource({ fields: { org: '4210', fund: '100000' } }, FIELDS)).toBe('Fund 100000 · Org 4210');
    expect(describeFundingSource({ label: 'Dean', fields: { fund: '100000' } }, FIELDS)).toBe('Dean (Fund 100000)');
    expect(describeFundingSource({ label: 'Dean', fields: {} }, FIELDS)).toBe('Dean');
    expect(describeFundingSource(null, FIELDS)).toBe('');
  });

  it('checks required and whole-value patterns like the backend', () => {
    expect(fundingFieldError(FIELDS[0], '')).toBe('Fund is required');
    expect(fundingFieldError(FIELDS[0], '1234567')).toBe('Fund is not in the expected format');
    expect(fundingFieldError(FIELDS[0], ' 100000 ')).toBeNull();
    expect(fundingFieldError(FIELDS[1], '')).toBeNull();
    // A pattern the browser cannot compile is left to the server.
    expect(fundingFieldError({ key: 'x', label: 'X', required: false, pattern: '(' }, 'a')).toBeNull();
  });

  it('drops blank funding values', () => {
    expect(cleanFundingValues({ fund: ' 100000 ', org: '  ' })).toEqual({ fund: '100000' });
  });

  it('names the user, the fund and the cap in the standing confirmation', () => {
    const capped = standingConfirmation({ user: 'Riley', fund: 'Fund 100000', cap: 200, through: '2027-06-30', per: 'per month' });
    expect(capped).toBe("After their included quota, Riley's usage will be charged to Fund 100000, up to $200 per month, through Jun 30, 2027.");
    const uncapped = standingConfirmation({ user: 'Riley', fund: 'Fund 100000', cap: null, through: '2027-06-30', per: 'per month' });
    expect(uncapped).toContain('with no cap');
  });

  it('words amounts for one-time and standing extensions', () => {
    expect(describeExtensionAmount(25, false)).toBe('+$25 per month');
    expect(describeExtensionAmount(200, true)).toBe('Cap $200 per month');
    expect(describeExtensionAmount(null, true)).toBe('No cap');
  });

  it('reads the age of a request, including the legacy +00:00Z spelling', () => {
    const now = new Date('2026-10-31T12:00:00Z');
    expect(age('2026-10-31T10:00:00+00:00Z', now)).toBe('2 h');
    expect(age('2026-10-31T11:30:00Z', now)).toBe('30 min');
    expect(age('2026-10-27T12:00:00Z', now)).toBe('4 d');
  });
});
