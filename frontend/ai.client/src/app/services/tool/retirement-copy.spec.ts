import { describe, it, expect } from 'vitest';
import { formatRetiresOn, retirementDetail } from './tool.service';

/**
 * The sentence every retirement surface appends after its own lead-in
 * (docs/specs/mcp-server-retirement.md §7).
 *
 * Both fields are independently optional, so there are four shapes and all four
 * have to stay grammatical. This helper exists so the Customize card, the detail
 * page, the Designer notice and the schedule form cannot drift into four
 * phrasings of the same fact — these tests are what hold that.
 */
describe('formatRetiresOn', () => {
  it('renders an ISO date in the viewer locale', () => {
    expect(formatRetiresOn('2026-10-31')).toContain('2026');
  });

  it('does NOT shift the date backwards west of Greenwich', () => {
    // `new Date('2026-10-31')` is midnight UTC and prints as the 30th for every
    // US timezone. A retirement date that reads a day early is the one kind of
    // wrong that costs someone, so the helper parses at UTC noon instead.
    expect(formatRetiresOn('2026-10-31')).toContain('31');
  });

  it.each([null, undefined, '', 'soon', '10/31/2026', '2026-13-45'])(
    'returns null for %s rather than an Invalid Date',
    (v) => {
      expect(formatRetiresOn(v as string | null | undefined)).toBeNull();
    },
  );
});

describe('retirementDetail', () => {
  it('joins a note and a date into one sentence', () => {
    const out = retirementDetail({
      retirementNote: 'Replaced by Canvas for Faculty',
      retiresOn: '2026-10-31',
    });
    expect(out).toMatch(/^Replaced by Canvas for Faculty\. It stops working on /);
    expect(out.endsWith('.')).toBe(true);
  });

  it('uses the note alone when there is no date', () => {
    expect(retirementDetail({ retirementNote: 'No replacement — contact OIT' })).toBe(
      'No replacement — contact OIT.',
    );
  });

  it('uses the date alone when there is no note', () => {
    expect(retirementDetail({ retiresOn: '2026-10-31' })).toMatch(/^It stops working on /);
  });

  it('says NOTHING when the admin recorded neither', () => {
    // Deliberate: "no replacement is available" would be a claim we invented on
    // the admin's behalf. Absence of information is not information.
    expect(retirementDetail({})).toBe('');
    expect(retirementDetail({ retirementNote: null, retiresOn: null })).toBe('');
    expect(retirementDetail({ retirementNote: '   ' })).toBe('');
  });

  it('does not double the full stop when an admin punctuated the note', () => {
    expect(retirementDetail({ retirementNote: 'Use the new one.' })).toBe('Use the new one.');
    expect(retirementDetail({ retirementNote: 'Use the new one.', retiresOn: '2026-10-31' })).toMatch(
      /^Use the new one\. It stops working on /,
    );
  });

  it('drops an unparseable date rather than printing it raw', () => {
    expect(retirementDetail({ retirementNote: 'Use X', retiresOn: 'soon' })).toBe('Use X.');
  });
});
