import { describe, it, expect } from 'vitest';
import { pdfPageRef, type PageCitation } from './pdf-page-link';

function cite(over: Partial<PageCitation> = {}): PageCitation {
  return {
    state: 'supported',
    locator: { kind: 'page', value: 7, precision: 'measured' },
    uploadId: 'up-1',
    filename: 'award.pdf',
    quote: 'The total approved budget is $487,500.',
    ...over,
  };
}

describe('pdfPageRef', () => {
  it('opens a located page of a PDF with its quote', () => {
    expect(pdfPageRef(cite())).toEqual({
      uploadId: 'up-1',
      filename: 'award.pdf',
      page: 7,
      quote: 'The total approved budget is $487,500.',
    });
  });

  it('links every located state, the unsupported one included', () => {
    for (const state of ['supported', 'quote_unsupported', 'unassessed'] as const) {
      expect(pdfPageRef(cite({ state }))?.page).toBe(7);
    }
  });

  it('keeps an estimated page', () => {
    expect(pdfPageRef(cite({ locator: { kind: 'page', value: 3, precision: 'estimated' } }))?.page).toBe(3);
  });

  it('omits a blank or missing quote', () => {
    expect(pdfPageRef(cite({ quote: '  ' }))).not.toHaveProperty('quote');
    expect(pdfPageRef(cite({ quote: null }))).not.toHaveProperty('quote');
  });

  it('refuses what cannot be opened at a page', () => {
    expect(pdfPageRef(cite({ state: 'unverified' }))).toBeNull();
    expect(pdfPageRef(cite({ locator: null }))).toBeNull();
    expect(pdfPageRef(cite({ locator: { kind: 'paragraph', value: 2, precision: 'measured' } }))).toBeNull();
    expect(pdfPageRef(cite({ filename: 'deck.pptx' }))).toBeNull();
    expect(pdfPageRef(cite({ uploadId: '' }))).toBeNull();
    expect(pdfPageRef(cite({ locator: { kind: 'page', value: 0, precision: 'measured' } }))).toBeNull();
    expect(pdfPageRef(cite({ locator: { kind: 'page', value: 2.5, precision: 'measured' } }))).toBeNull();
  });
});
