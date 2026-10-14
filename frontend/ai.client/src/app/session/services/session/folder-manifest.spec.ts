import { describe, expect, it } from 'vitest';
import { folderDataFromRow, parseFolderManifests } from './folder-manifest';
import type { FolderAttachment } from '../../../services/folder-attachments';

/**
 * The manifest text below is exactly what the backend renders
 * (`backend/tests/shared/test_folder_manifest.py` pins the same strings), so a
 * change on either side that breaks the chip on reload fails here.
 */
const READY = [
  'what is due?',
  '',
  '<folder name="BIO 101 Spring" source="Google Drive" status="ready" files="2">',
  '  syllabus.pdf — upload_id=up-1 — 1.2 MB — pdf',
  '  week 1/rubric.docx — upload_id=up-2 — 84 KB — docx',
  '</folder>',
  '',
  "_The folder's files are attached, but their content is not in this message. To answer from a document in it, call `document_read` with that file's `upload_id`: first with `pattern` to find the relevant passages or pages, then read them (`page_range` for a PDF). To find which file holds something, use `pattern` on the likeliest files._",
].join('\n');

describe('parseFolderManifests', () => {
  it('reads a ready folder and strips the block and its guidance', () => {
    const { folders, text } = parseFolderManifests(READY);
    expect(folders).toEqual([
      {
        name: 'BIO 101 Spring',
        source: 'Google Drive',
        status: 'ready',
        fileCount: 2,
        files: ['syllabus.pdf', 'week 1/rubric.docx'],
        moreCount: 0,
      },
    ]);
    expect(text).toBe('what is due?');
  });

  it('reads the files marker untouched, so file cards still restore', () => {
    const { text } = parseFolderManifests(`${READY}\n\n[Attached files: notes.pdf]`);
    expect(text).toBe('what is due?\n\n[Attached files: notes.pdf]');
  });

  it('counts the files beyond the bound and handles a listing without handles', () => {
    const block = [
      '<folder name="Big" source="Google Drive" status="partial" files="53" listed="60" skipped="7">',
      '  a.pdf — 1 KB — pdf',
      '  … and 52 more',
      '</folder>',
    ].join('\n');
    const [folder] = parseFolderManifests(block).folders;
    expect(folder.files).toEqual(['a.pdf']);
    expect(folder.moreCount).toBe(52);
    expect(folder.fileCount).toBe(53);
    expect(folder.status).toBe('partial');
  });

  it('reads a self-closing failed folder', () => {
    const { folders, text } = parseFolderManifests(
      'hi\n\n<folder name="X" source="google-drive" status="failed" files="0" listed="0" error="needs_consent"/>\n\n_The attached folder has no readable files yet (still being added, or none could be read). Tell the user what its status says rather than guessing at its contents._',
    );
    expect(folders[0]).toMatchObject({ name: 'X', status: 'failed', fileCount: 0, files: [] });
    expect(text).toBe('hi');
  });

  it('unescapes names and paths', () => {
    const { folders } = parseFolderManifests(
      '<folder name="Q3 &quot;plans&quot; &lt;draft&gt;" source="Drive" status="ready" files="1">\n  notes/&lt;a&gt; &amp; b.md — upload_id=u — 1 KB — md\n</folder>',
    );
    expect(folders[0].name).toBe('Q3 "plans" <draft>');
    expect(folders[0].files).toEqual(['notes/<a> & b.md']);
  });

  it('reads two folders in order', () => {
    const two = `${READY.split('\n\n_')[0]}\n\n<folder name="Second" source="Drive" status="ready" files="0"/>`;
    expect(parseFolderManifests(two).folders.map((f) => f.name)).toEqual(['BIO 101 Spring', 'Second']);
  });

  it('leaves text without a folder alone', () => {
    expect(parseFolderManifests('just a question')).toEqual({ folders: [], text: 'just a question' });
  });
});

describe('folderDataFromRow', () => {
  it('lists only attached files, by path', () => {
    const row = {
      folderAttachId: 'fa-1',
      folderName: 'Course',
      connectorName: 'Google Drive',
      status: 'expanding',
      files: [
        { relativePath: 'a.pdf', status: 'attached', uploadId: 'u1' },
        { relativePath: 'b.pdf', status: 'pending', uploadId: null },
      ],
    } as unknown as FolderAttachment;
    expect(folderDataFromRow(row)).toEqual({
      folderAttachId: 'fa-1',
      name: 'Course',
      source: 'Google Drive',
      status: 'expanding',
      fileCount: 1,
      files: ['a.pdf'],
      moreCount: 0,
    });
  });
});
