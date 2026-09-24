import { describe, expect, it } from 'vitest';

import { findHighlightRanges, toSegments } from './composer-highlights';

describe('findHighlightRanges', () => {
  it('marks the picked agent’s @Name, names with spaces included', () => {
    const text = '@Rubric Builder tighten the analysis criterion';
    expect(findHighlightRanges(text, 'Rubric Builder', [])).toEqual([[0, 15]]);
  });

  it('leaves an @Name alone when no agent was picked', () => {
    expect(findHighlightRanges('ask @Rubric Builder', null, [])).toEqual([]);
  });

  it('marks nothing once the user has edited the name away', () => {
    expect(findHighlightRanges('@Rubric Build tighten it', 'Rubric Builder', [])).toEqual([]);
  });

  it('marks known /skills that start a word, and only those', () => {
    const text = 'Run /pdf-workflows on the 3/4 draft and/or /unknown';
    expect(findHighlightRanges(text, null, ['pdf-workflows'])).toEqual([[4, 18]]);
  });

  it('does not mark a slug inside a path or URL', () => {
    expect(findHighlightRanges('see docs/pdf-workflows/readme', null, ['pdf-workflows'])).toEqual([]);
  });

  it('returns mention and skills in text order', () => {
    const text = '/brand-deck for @Deck Agent please';
    expect(findHighlightRanges(text, 'Deck Agent', ['brand-deck'])).toEqual([
      [0, 11],
      [16, 27],
    ]);
  });
});

describe('toSegments', () => {
  it('is null when nothing is marked, so the mirror layer is not painted', () => {
    expect(toSegments('plain text', [])).toBeNull();
  });

  it('splits around each range and keeps every character', () => {
    const text = 'a /x b';
    const segments = toSegments(text, [[2, 4]])!;
    expect(segments).toEqual([
      { text: 'a ', mark: false },
      { text: '/x', mark: true },
      { text: ' b', mark: false },
    ]);
    expect(segments.map(s => s.text).join('')).toBe(text);
  });
});
