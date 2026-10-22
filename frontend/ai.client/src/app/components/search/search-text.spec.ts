import { describe, expect, it } from 'vitest';
import { formatLastMoved, highlightSegments, queryTerms } from './search-text';

describe('search text helpers', () => {
  it('queryTerms: lower-cased, de-duplicated, longest first, short words dropped', () => {
    expect(queryTerms('Window a WINDOW sql')).toEqual(['window', 'sql']);
  });

  it('highlightSegments marks every query word, case-insensitively', () => {
    expect(highlightSegments('Use a Window function over SQL rows', 'window sql')).toEqual([
      { text: 'Use a ', match: false },
      { text: 'Window', match: true },
      { text: ' function over ', match: false },
      { text: 'SQL', match: true },
      { text: ' rows', match: false },
    ]);
  });

  it('treats regex characters in the query literally', () => {
    expect(highlightSegments('cost is $5 (est.)', '(est.)')).toEqual([
      { text: 'cost is $5 ', match: false },
      { text: '(est.)', match: true },
    ]);
  });

  it('returns one plain run when nothing can be marked', () => {
    expect(highlightSegments('hello', 'a')).toEqual([{ text: 'hello', match: false }]);
    expect(highlightSegments('', 'x')).toEqual([]);
  });

  it('formatLastMoved uses the sidebar wording', () => {
    const now = new Date('2026-10-07T12:00:00Z');
    expect(formatLastMoved('2026-10-07T11:59:40Z', now)).toBe('Just now');
    expect(formatLastMoved('2026-10-07T11:55:00Z', now)).toBe('5m ago');
    expect(formatLastMoved('2026-10-07T09:00:00Z', now)).toBe('3h ago');
    expect(formatLastMoved('2026-10-05T12:00:00Z', now)).toBe('2d ago');
    expect(formatLastMoved('2026-09-01T12:00:00Z', now)).toBe('Sep 1');
    expect(formatLastMoved('garbage', now)).toBe('');
  });
});
