import { describe, it, expect } from 'vitest';
import {
  isPreviewableFilename,
  PREVIEW_KIND_LABELS,
  previewFetchesBytes,
  previewKindFor,
} from './file-preview.model';

describe('previewKindFor', () => {
  it('maps the formats the pane can render', () => {
    expect(previewKindFor('plan.docx')).toBe('docx');
    expect(previewKindFor('deck.pptx')).toBe('pptx');
    expect(previewKindFor('rows.csv')).toBe('csv');
    expect(previewKindFor('budget.xlsx')).toBe('xlsx');
  });

  it('ignores case and surrounding whitespace', () => {
    expect(previewKindFor('  REPORT.DOCX ')).toBe('docx');
    expect(previewKindFor('Quarterly Review.PPTX')).toBe('pptx');
    expect(previewKindFor('Export.CSV')).toBe('csv');
    expect(previewKindFor('Budget FY27.XLSX')).toBe('xlsx');
  });

  it('declines the pre-2007 binary formats', () => {
    // Not OOXML at all — the renderers cannot read them, so offering a
    // preview would only produce an error the user cannot act on.
    expect(previewKindFor('old.doc')).toBeNull();
    expect(previewKindFor('old.ppt')).toBeNull();
    expect(previewKindFor('old.xls')).toBeNull();
  });

  it('reads .xlsx server-side rather than from bytes', () => {
    // The one kind whose bytes never reach the browser: no client-side
    // workbook renderer was shippable, so app-api reads it instead. A
    // panel that ran its presigned-URL fetch for this kind would
    // download the whole workbook and then ignore it.
    expect(previewFetchesBytes('xlsx')).toBe(false);
  });

  it('reads every other kind from bytes it fetched', () => {
    for (const kind of ['docx', 'pptx', 'csv'] as const) {
      expect(previewFetchesBytes(kind)).toBe(true);
    }
  });

  it('declines an extension that merely contains a known one', () => {
    expect(previewKindFor('plan.docx.pdf')).toBeNull();
    expect(previewKindFor('notdocx')).toBeNull();
  });

  it('agrees with isPreviewableFilename', () => {
    for (const name of [
      'a.docx',
      'b.pptx',
      'c.xlsx',
      'd.txt',
      'e.doc',
      'f.csv',
      'g.xls',
    ]) {
      expect(isPreviewableFilename(name)).toBe(previewKindFor(name) !== null);
    }
  });

  it('labels every kind it can return', () => {
    for (const name of ['a.docx', 'b.pptx', 'c.csv', 'd.xlsx']) {
      const kind = previewKindFor(name);
      expect(kind).not.toBeNull();
      expect(PREVIEW_KIND_LABELS[kind!]).toBeTruthy();
    }
  });
});
