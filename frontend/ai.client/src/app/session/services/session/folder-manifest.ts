import type { FolderAttachmentData } from '../models/message.model';
import {
  attachedFolderFiles,
  type FolderAttachment,
} from '../../../services/folder-attachments';

/**
 * The folder manifest as the Runtime writes it into a user message
 * (`backend/src/apis/shared/files/folder_manifest.py`, pinned there by exact-text
 * tests), read back for the folder chip on reload:
 *
 *   <folder name="BIO 101" source="Google Drive" status="ready" files="2">
 *     syllabus.pdf — upload_id=… — 1 KB — pdf
 *     week 1/rubric.docx — upload_id=… — 84 KB — docx
 *     … and 12 more; call `document_read` with no arguments to list them all
 *   </folder>
 *
 * or self-closing when nothing is attached. Then one guidance line in italics.
 * The block is the message's only record of the folder, and it is exactly what
 * the model was given, so the chip shows the same list.
 */

const FOLDER_BLOCK = /<folder\s([^>]*?)(\/>|>([\s\S]*?)<\/folder>)/g;
const ATTRIBUTE = /(\w+)="([^"]*)"/g;
const MORE_LINE = /^…\s+and\s+(\d+)\s+more/;
/** The guidance line's three openings (`folder_guidance` in the backend). */
const GUIDANCE_LINE = /^_(The folder's files|These folders' files|The attached folder has no readable files)[^\n]*_$/gm;

function unescape(text: string): string {
  return text
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function attributes(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const match of raw.matchAll(ATTRIBUTE)) {
    out[match[1]] = unescape(match[2]);
  }
  return out;
}

/** Folders named in `text`, and the text with their blocks and guidance removed. */
export function parseFolderManifests(text: string): { folders: FolderAttachmentData[]; text: string } {
  const folders: FolderAttachmentData[] = [];
  for (const match of text.matchAll(FOLDER_BLOCK)) {
    const attrs = attributes(match[1]);
    const files: string[] = [];
    let moreCount = 0;
    for (const rawLine of (match[3] ?? '').split('\n')) {
      const line = rawLine.trim();
      if (!line) continue;
      const more = MORE_LINE.exec(line);
      if (more) {
        moreCount = Number(more[1]);
        continue;
      }
      files.push(unescape(line.split(' — ')[0]));
    }
    folders.push({
      name: attrs['name'] ?? 'Folder',
      source: attrs['source'] ?? null,
      status: attrs['status'] ?? 'ready',
      fileCount: Number(attrs['files'] ?? files.length + moreCount) || 0,
      files,
      moreCount,
    });
  }
  if (folders.length === 0) {
    return { folders, text };
  }
  const stripped = text
    .replace(FOLDER_BLOCK, '')
    .replace(GUIDANCE_LINE, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  return { folders, text: stripped };
}

/** The chip's data for a folder sent from this tab, from its row at send time. */
export function folderDataFromRow(row: FolderAttachment): FolderAttachmentData {
  const attached = attachedFolderFiles(row);
  return {
    folderAttachId: row.folderAttachId,
    name: row.folderName,
    source: row.connectorName ?? null,
    status: row.status,
    fileCount: attached.length,
    files: attached.map((file) => file.relativePath),
    moreCount: 0,
  };
}
