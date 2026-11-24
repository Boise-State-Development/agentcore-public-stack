import { describe, it, expect } from 'vitest';
import { findQuote, type QuoteRange } from './pdf-quote-match';

/** The text each range covers, to assert on words rather than offsets. */
function covered(texts: string[], ranges: QuoteRange[]): string[] {
  return ranges.map((r) => texts[r.text].slice(r.start, r.end));
}

describe('findQuote', () => {
  it('finds a quote inside one run', () => {
    const texts = ['The total approved budget is $487,500 for this award.'];
    const ranges = findQuote(texts, 'approved budget is $487,500');
    expect(covered(texts, ranges)).toEqual(['approved budget is $487,500']);
  });

  it('finds a quote spread over several runs, one range per run', () => {
    const texts = ['Line 4: the quick', 'brown fox jumps', 'over the lazy dog.'];
    const ranges = findQuote(texts, 'quick brown fox jumps over');
    expect(covered(texts, ranges)).toEqual(['quick', 'brown fox jumps', 'over']);
    expect(ranges.map((r) => r.text)).toEqual([0, 1, 2]);
  });

  it('ignores how the two extractions spaced the text', () => {
    // pdf.js may split words into runs with no space between them, or
    // break a line where the server's text had a space.
    const texts = ['the', 'quick   brown'];
    expect(covered(texts, findQuote(texts, 'the quick brown'))).toEqual(['the', 'quick   brown']);
    expect(findQuote(['the quick\nbrown'], 'thequick brown')).toHaveLength(1);
  });

  it('folds the characters the server folds', () => {
    const texts = ['The “final” report — signed by the ofﬁce.'];
    const ranges = findQuote(texts, 'The "final" report - signed by the office.');
    expect(covered(texts, ranges)).toEqual([texts[0]]);
  });

  it('skips soft hyphens and zero-width characters', () => {
    const texts = ['a verifi­cation step and a zero​width'];
    expect(findQuote(texts, 'verification step')).toHaveLength(1);
    expect(findQuote(texts, 'zerowidth')).toHaveLength(1);
  });

  it('is case-insensitive', () => {
    expect(findQuote(['TOTAL AWARD'], 'total award')).toHaveLength(1);
  });

  it('returns nothing for a quote that is not on the page', () => {
    expect(findQuote(['The quick brown fox.'], 'a lazy dog')).toEqual([]);
    expect(findQuote(['anything'], '   ')).toEqual([]);
    expect(findQuote([], 'anything')).toEqual([]);
  });

  it('highlights the part of a page-straddling quote that ends this page', () => {
    const texts = ['Intro text.', 'The award period runs from March 1 through'];
    const quote = 'The award period runs from March 1 through August 31, 2027.';
    const ranges = findQuote(texts, quote);
    expect(covered(texts, ranges)).toEqual(['The award period runs from March 1 through']);
  });

  it('does not take a few shared letters at the foot of a page for the quote', () => {
    // The page ends with "the", which does start the quote, but three
    // letters in common are a coincidence, not a passage.
    expect(findQuote(['ends with the'], 'the award period was extended')).toEqual([]);
  });
});
