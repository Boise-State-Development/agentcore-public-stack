import { describe, it, expect } from 'vitest';
import {
  isPreviewableFilename,
  PREVIEW_KIND_LABELS,
  previewKindFor,
} from './file-preview.model';

describe('previewKindFor', () => {
  it('maps the OOXML formats the pane can render', () => {
    expect(previewKindFor('plan.docx')).toBe('docx');
    expect(previewKindFor('deck.pptx')).toBe('pptx');
  });

  it('ignores case and surrounding whitespace', () => {
    expect(previewKindFor('  REPORT.DOCX ')).toBe('docx');
    expect(previewKindFor('Quarterly Review.PPTX')).toBe('pptx');
  });

  it('declines the pre-2007 binary formats', () => {
    // Not OOXML at all — the renderers cannot read them, so offering a
    // preview would only produce an error the user cannot act on.
    expect(previewKindFor('old.doc')).toBeNull();
    expect(previewKindFor('old.ppt')).toBeNull();
  });

  it('declines .xlsx', () => {
    // Deliberate: there is no spreadsheet renderer we are willing to
    // ship. See the note on previewKindFor.
    expect(previewKindFor('budget.xlsx')).toBeNull();
  });

  it('declines an extension that merely contains a known one', () => {
    expect(previewKindFor('plan.docx.pdf')).toBeNull();
    expect(previewKindFor('notdocx')).toBeNull();
  });

  it('agrees with isPreviewableFilename', () => {
    for (const name of ['a.docx', 'b.pptx', 'c.xlsx', 'd.txt', 'e.doc']) {
      expect(isPreviewableFilename(name)).toBe(previewKindFor(name) !== null);
    }
  });

  it('labels every kind it can return', () => {
    for (const name of ['a.docx', 'b.pptx']) {
      const kind = previewKindFor(name);
      expect(kind).not.toBeNull();
      expect(PREVIEW_KIND_LABELS[kind!]).toBeTruthy();
    }
  });
});
