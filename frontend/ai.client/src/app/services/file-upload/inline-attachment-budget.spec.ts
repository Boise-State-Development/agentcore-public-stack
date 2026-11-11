import { describe, expect, it } from 'vitest';
import { FALLBACK_ATTACHMENT_LIMITS as LIMITS, type AttachmentLimits } from './attachment-limits';
import { type BudgetedAttachment, countsTowardInlineBudget, inlineBudgetOverflow } from './inline-attachment-budget';

const MB = 1_000_000;
const PDF = 'application/pdf';
const NO_BUDGET: AttachmentLimits = { ...LIMITS, inlineBudgetBytes: null };

function file(id: string, filename: string, sizeBytes: number, mimeType = PDF): BudgetedAttachment {
  return { id, filename, mimeType, sizeBytes };
}

describe('inlineBudgetOverflow', () => {
  it('is null while every attachment fits', () => {
    expect(inlineBudgetOverflow([file('a', 'a.pdf', 3.9 * MB), file('b', 'b.pdf', 2.7 * MB)], LIMITS)).toBeNull();
    expect(inlineBudgetOverflow([], LIMITS)).toBeNull();
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
    ], LIMITS);

    expect(over).toEqual({ byReference: [file('d', 'policy-d.pdf', 4.0 * MB)], dropped: [] });
  });

  it('counts images and leaves an image that does not fit out entirely', () => {
    const over = inlineBudgetOverflow([
      file('a', 'scan.pdf', 3.9 * MB),
      file('b', 'photo.png', 3.5 * MB, 'image/png'),
    ], LIMITS);

    expect(over).toEqual({ byReference: [], dropped: [file('b', 'photo.png', 3.5 * MB, 'image/png')] });
  });

  it('allows a total exactly at the budget', () => {
    expect(
      inlineBudgetOverflow([
        file('a', 'a.pdf', (LIMITS.inlineBudgetBytes as number) - 1),
        file('b', 'b.txt', 1, 'text/plain'),
      ], LIMITS),
    ).toBeNull();
  });

  it('classifies by extension when the browser reports no type', () => {
    const over = inlineBudgetOverflow([file('a', 'a.pdf', 3.9 * MB), file('b', 'b.docx', 3.9 * MB, '')], LIMITS);

    expect(over?.byReference.map(f => f.filename)).toEqual(['b.docx']);
  });

  it('is disabled when the profile has no budget', () => {
    expect(inlineBudgetOverflow([file('a', 'a.pdf', 3.9 * MB), file('b', 'b.pdf', 3.9 * MB)], NO_BUDGET)).toBeNull();
  });
});

describe('countsTowardInlineBudget', () => {
  it('skips what never rides inline at any size', () => {
    expect(countsTowardInlineBudget('data.csv', 'text/csv', 3 * MB, LIMITS)).toBe(false);
    expect(countsTowardInlineBudget('data.xlsx', '', 3 * MB, LIMITS)).toBe(false);
    expect(
      countsTowardInlineBudget('deck.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 3 * MB, LIMITS),
    ).toBe(false);
    // Over the per-file inline limit: attached by reference on its own.
    expect(countsTowardInlineBudget('manual.pdf', PDF, 5 * MB, LIMITS)).toBe(false);
  });

  it('counts images and inline-class documents', () => {
    expect(countsTowardInlineBudget('photo.jpg', 'image/jpeg', 3 * MB, LIMITS)).toBe(true);
    expect(countsTowardInlineBudget('policy.pdf', PDF, 3 * MB, LIMITS)).toBe(true);
    expect(countsTowardInlineBudget('query.sql', '', 1 * MB, LIMITS)).toBe(true);
  });
});

describe('under the token profile', () => {
  // As dev served it: images are downscaled at upload, so the uploaded bytes never travel.
  const TOKEN: AttachmentLimits = {
    ...LIMITS,
    profile: 'token',
    filesPerMessage: 20,
    imagesPerMessage: 20,
    pdfMaxPages: 1000,
    maxBytes: { ...LIMITS.maxBytes, image: 20 * 1024 * 1024 },
  };

  it('never leaves an image out, whatever its uploaded size', () => {
    // The dev validation's case: a 10 MB photo among nineteen small files.
    const photo = file('p', 'image_8_photo.jpg', 10.2 * MB, 'image/jpeg');
    const notes = Array.from({ length: 11 }, (_, i) => file(`n${i}`, `note_${i}.md`, 0.009 * MB, 'text/markdown'));
    expect(inlineBudgetOverflow([photo, ...notes, file('r', 'report.pdf', 1.3 * MB)], TOKEN)).toBeNull();
    expect(countsTowardInlineBudget('photo.jpg', 'image/jpeg', 19 * MB, TOKEN)).toBe(false);
  });

  it('still walks the documents', () => {
    const over = inlineBudgetOverflow([
      file('p', 'photo.jpg', 15 * MB, 'image/jpeg'),
      file('a', 'a.pdf', 3.9 * MB),
      file('b', 'b.pdf', 3.9 * MB),
    ], TOKEN);
    expect(over).toEqual({ byReference: [file('b', 'b.pdf', 3.9 * MB)], dropped: [] });
  });
});

describe('limits from GET /files/limits', () => {
  it('reads the served budget, not a built-in one', () => {
    const tight: AttachmentLimits = { ...LIMITS, inlineBudgetBytes: 5 * MB };
    expect(inlineBudgetOverflow([file('a', 'a.pdf', 3 * MB), file('b', 'b.pdf', 3 * MB)], LIMITS)).toBeNull();
    expect(inlineBudgetOverflow([file('a', 'a.pdf', 3 * MB), file('b', 'b.pdf', 3 * MB)], tight)?.byReference).toHaveLength(1);
  });

  it('treats a document over the served inline limit as by reference', () => {
    const smaller: AttachmentLimits = { ...LIMITS, inlineDocumentMaxBytes: 2 * MB };
    expect(countsTowardInlineBudget('a.pdf', PDF, 3 * MB, LIMITS)).toBe(true);
    expect(countsTowardInlineBudget('a.pdf', PDF, 3 * MB, smaller)).toBe(false);
  });
});
