import { describe, it, expect } from 'vitest';
import { deriveTagline, TAGLINE_MAX } from './store.model';

/**
 * Tagline derivation (D4 backfill, #749).
 *
 * `tagline` postdates every Agent built before the Marketplace, and it was author-owned
 * but never author-settable — no Designer control wrote it. Submission prefills from the
 * description so a legacy Agent is not asked to invent a field it never had, and so the
 * author sees what the shelf will say at the moment they can still change it.
 *
 * The bar is "a sensible starting point the author will edit", not "correct prose". What
 * these pin is that it never emits something *worse* than the truncated description the
 * store falls back to today: no mid-word breaks, no trailing punctuation, never over the
 * limit.
 */
describe('deriveTagline', () => {
  it('returns nothing for an empty description', () => {
    expect(deriveTagline(undefined)).toBe('');
    expect(deriveTagline(null)).toBe('');
    expect(deriveTagline('   ')).toBe('');
  });

  it('uses a short description whole', () => {
    expect(deriveTagline('Finds and cites university policy')).toBe(
      'Finds and cites university policy',
    );
  });

  it('drops trailing punctuation', () => {
    expect(deriveTagline('Finds and cites university policy.')).toBe(
      'Finds and cites university policy',
    );
  });

  it('collapses whitespace, including newlines', () => {
    expect(deriveTagline('Finds  and\n  cites\tpolicy')).toBe('Finds and cites policy');
  });

  it('cuts at the first sentence when the description runs long', () => {
    const text =
      'Finds and cites university policy. It also drafts summaries for department chairs and '
      + 'answers questions about the travel reimbursement process.';
    expect(deriveTagline(text)).toBe('Finds and cites university policy');
  });

  it('cuts at a semicolon clause', () => {
    const text =
      'Answers questions about travel reimbursement; it reads the policy manual and the '
      + 'current per-diem tables before replying to anyone.';
    expect(deriveTagline(text)).toBe('Answers questions about travel reimbursement');
  });

  it('never exceeds the limit', () => {
    const text = 'x'.repeat(500);
    expect(deriveTagline(text).length).toBeLessThanOrEqual(TAGLINE_MAX);
  });

  it('breaks on a whole word when there is no clause boundary', () => {
    // The fallback case — this is the one that would otherwise read as a truncation.
    const text =
      'A research assistant for graduate students working on federally funded grant '
      + 'proposals across every college in the university';
    const result = deriveTagline(text);

    expect(result.length).toBeLessThanOrEqual(TAGLINE_MAX);
    // Whatever it cut, it cut between words — the property that matters.
    expect(text.startsWith(result)).toBe(true);
    expect(text[result.length]).toBe(' ');
  });

  it('ignores a boundary too early to be a useful subtitle', () => {
    // "Hi." is a clause, but a three-character tagline is not a subtitle.
    const text = 'Hi. This agent helps students find and understand university policy quickly.';
    expect(deriveTagline(text).length).toBeGreaterThan(20);
  });
});
