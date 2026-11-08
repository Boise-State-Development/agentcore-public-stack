import { describe, expect, it } from 'vitest';
import { INLINE_ATTACHMENTS_MAX_TOTAL_BYTES } from './file-upload.service';
import { type BudgetedAttachment, countsTowardInlineBudget, inlineBudgetOverflow } from './inline-attachment-budget';

const MB = 1_000_000;
const PDF = 'application/pdf';

function file(id: string, filename: string, sizeBytes: number, mimeType = PDF): BudgetedAttachment {
  return { id, filename, mimeType, sizeBytes };
}

describe('inlineBudgetOverflow', () => {
  it('is null while every attachment fits', () => {
    expect(inlineBudgetOverflow([file('a', 'a.pdf', 3.9 * MB), file('b', 'b.pdf', 2.7 * MB)])).toBeNull();
    expect(inlineBudgetOverflow([])).toBeNull();
  });

  it('walks first-fit in attachment order, as the backend does', () => {
    // Five policy PDFs: the first goes by reference on its own (over 4 MiB), and
    // the fourth is the inline candidate that pushes the total past 7 MB. The
    // fifth, smaller, still fits.
    const over = inlineBudgetOverflow([
      file('a', 'policy-a.pdf', 5.7 * MB),
      file('b', 'policy-b.pdf', 3.9 * MB),
      file('c', 'policy-c.pdf', 0.17 * MB),
      file('d', 'policy-d.pdf', 4.0 * MB),
      file('e', 'policy-e.pdf', 2.7 * MB),
    ]);

    expect(over).toEqual({ byReference: [file('d', 'policy-d.pdf', 4.0 * MB)], dropped: [] });
  });

  it('counts images and leaves an image that does not fit out entirely', () => {
    const over = inlineBudgetOverflow([
      file('a', 'scan.pdf', 3.9 * MB),
      file('b', 'photo.png', 3.5 * MB, 'image/png'),
    ]);

    expect(over).toEqual({ byReference: [], dropped: [file('b', 'photo.png', 3.5 * MB, 'image/png')] });
  });

  it('allows a total exactly at the budget', () => {
    expect(
      inlineBudgetOverflow([
        file('a', 'a.pdf', INLINE_ATTACHMENTS_MAX_TOTAL_BYTES - 1),
        file('b', 'b.txt', 1, 'text/plain'),
      ]),
    ).toBeNull();
  });

  it('classifies by extension when the browser reports no type', () => {
    const over = inlineBudgetOverflow([file('a', 'a.pdf', 3.9 * MB), file('b', 'b.docx', 3.9 * MB, '')]);

    expect(over?.byReference.map(f => f.filename)).toEqual(['b.docx']);
  });

  it('is disabled by a non-positive budget', () => {
    expect(inlineBudgetOverflow([file('a', 'a.pdf', 3.9 * MB), file('b', 'b.pdf', 3.9 * MB)], 0)).toBeNull();
  });
});

describe('countsTowardInlineBudget', () => {
  it('skips what never rides inline at any size', () => {
    expect(countsTowardInlineBudget('data.csv', 'text/csv', 3 * MB)).toBe(false);
    expect(countsTowardInlineBudget('data.xlsx', '', 3 * MB)).toBe(false);
    expect(
      countsTowardInlineBudget('deck.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 3 * MB),
    ).toBe(false);
    // Over the per-file inline limit: attached by reference on its own.
    expect(countsTowardInlineBudget('manual.pdf', PDF, 5 * MB)).toBe(false);
  });

  it('counts images and inline-class documents', () => {
    expect(countsTowardInlineBudget('photo.jpg', 'image/jpeg', 3 * MB)).toBe(true);
    expect(countsTowardInlineBudget('policy.pdf', PDF, 3 * MB)).toBe(true);
    expect(countsTowardInlineBudget('query.sql', '', 1 * MB)).toBe(true);
  });
});
