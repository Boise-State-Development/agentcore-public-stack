import { describe, it, expect } from 'vitest';
import {
  isPreviewableFilename,
  isSamePreviewDocument,
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
    expect(previewKindFor('report.pdf')).toBe('pdf');
  });

  it('ignores case and surrounding whitespace', () => {
    expect(previewKindFor('  REPORT.DOCX ')).toBe('docx');
    expect(previewKindFor('Quarterly Review.PPTX')).toBe('pptx');
    expect(previewKindFor('Export.CSV')).toBe('csv');
    expect(previewKindFor('Budget FY27.XLSX')).toBe('xlsx');
    expect(previewKindFor(' Annual Report.PDF')).toBe('pdf');
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
    for (const kind of ['docx', 'pptx', 'csv', 'pdf'] as const) {
      expect(previewFetchesBytes(kind)).toBe(true);
    }
  });

  it('declines an extension that merely contains a known one', () => {
    expect(previewKindFor('plan.docx.bak')).toBeNull();
    expect(previewKindFor('scan.pdf.zip')).toBeNull();
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
      'h.pdf',
    ]) {
      expect(isPreviewableFilename(name)).toBe(previewKindFor(name) !== null);
    }
  });

  it('labels every kind it can return', () => {
    for (const name of ['a.docx', 'b.pptx', 'c.csv', 'd.xlsx', 'e.pdf']) {
      const kind = previewKindFor(name);
      expect(kind).not.toBeNull();
      expect(PREVIEW_KIND_LABELS[kind!]).toBeTruthy();
    }
  });
});

describe('isSamePreviewDocument', () => {
  it('treats another page of the same upload as the same document', () => {
    expect(
      isSamePreviewDocument(
        { uploadId: 'up1', filename: 'a.pdf' },
        { uploadId: 'up1', filename: 'a.pdf', page: 9 },
      ),
    ).toBe(true);
  });

  it('tells different uploads apart', () => {
    expect(
      isSamePreviewDocument(
        { uploadId: 'up1', filename: 'a.pdf' },
        { uploadId: 'up2', filename: 'a.pdf' },
      ),
    ).toBe(false);
  });

  it('never matches an upload to a knowledge-base document', () => {
    expect(
      isSamePreviewDocument(
        { uploadId: 'd1', filename: 'a.md' },
        { kind: 'kb-document', assistantId: 'a1', documentId: 'd1', filename: 'a.md' },
      ),
    ).toBe(false);
  });

  it('matches knowledge-base documents by agent and document', () => {
    const kb = { kind: 'kb-document' as const, assistantId: 'a1', documentId: 'd1', filename: 'a.md' };
    expect(isSamePreviewDocument(kb, { ...kb })).toBe(true);
    expect(isSamePreviewDocument(kb, { ...kb, documentId: 'd2' })).toBe(false);
  });

  it('handles a closed pane', () => {
    expect(isSamePreviewDocument(null, null)).toBe(true);
    expect(isSamePreviewDocument(null, { uploadId: 'up1', filename: 'a.pdf' })).toBe(false);
  });
});
