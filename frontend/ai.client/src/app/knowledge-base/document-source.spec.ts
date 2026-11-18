import { describe, expect, it } from 'vitest';
import { Document } from '../assistants/models/document.model';
import { documentOpenAction, documentSourceKind } from './document-source';

function doc(overrides: Partial<Document> = {}): Document {
  return {
    documentId: 'd',
    assistantId: 'a',
    filename: 'notes.pdf',
    contentType: 'application/pdf',
    sizeBytes: 1,
    status: 'complete',
    createdAt: '',
    updatedAt: '',
    ...overrides,
  } as Document;
}

describe('documentSourceKind', () => {
  it('reads where a document came from off its provenance', () => {
    expect(documentSourceKind(doc())).toBe('upload');
    expect(documentSourceKind(doc({ sourceConnectorId: 'web', sourceFileId: 'https://x' }))).toBe('web');
    expect(documentSourceKind(doc({ sourceConnectorId: 'google', sourceFileId: 'f1' }))).toBe('connector');
  });
});

describe('documentOpenAction', () => {
  it('links a Drive file to Drive, named for the connector, whatever its status', () => {
    const action = documentOpenAction(
      doc({ sourceConnectorId: 'google', sourceAdapterKey: 'google-drive', sourceFileId: 'abc_D-1', status: 'uploading' }),
      'Google Drive for Employees',
    );
    expect(action).toEqual({
      kind: 'link',
      href: 'https://drive.google.com/open?id=abc_D-1',
      label: 'Open in Google Drive for Employees',
    });
  });

  it('never builds a link from an id or URL it cannot trust', () => {
    expect(
      documentOpenAction(doc({ sourceConnectorId: 'google', sourceAdapterKey: 'google-drive', sourceFileId: 'x/../y' })).kind,
    ).toBe('file');
    expect(documentOpenAction(doc({ sourceConnectorId: 'web', sourceFileId: 'javascript:alert(1)' })).kind).toBe('file');
  });

  it('opens a web page at its URL', () => {
    expect(documentOpenAction(doc({ sourceConnectorId: 'web', sourceFileId: 'https://x.edu/a' }))).toMatchObject({
      kind: 'link',
      href: 'https://x.edu/a',
    });
  });

  it('views text in the app, renders markdown, and opens anything else as the file', () => {
    expect(documentOpenAction(doc({ filename: 'README.md' }))).toMatchObject({ kind: 'view', format: 'markdown' });
    expect(documentOpenAction(doc({ filename: 'data.csv' }))).toMatchObject({ kind: 'view', format: 'text' });
    expect(documentOpenAction(doc({ filename: 'report.pdf' }))).toMatchObject({ kind: 'file' });
  });

  it('offers nothing to open for a stored file that is not ready yet', () => {
    expect(documentOpenAction(doc({ filename: 'README.md', status: 'embedding' }))).toEqual({ kind: 'none' });
  });
});
