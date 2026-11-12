import { describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';

import { environment } from '../../../../environments/environment';
import { FEATURES } from '../../../services/features';
import {
  AttachmentPreviewsService,
  FileUploadService,
  type AttachmentPreview,
  type FileMetadata,
  type PendingUpload,
} from '../../../services/file-upload';
import { FilePreviewStateService } from '../../services/file-preview/file-preview-state.service';
import type { FileAttachmentData } from '../../services/models/message.model';
import { AttachmentCardComponent } from './attachment-card.component';
import { csvPreviewRows } from './attachment-kind';
import { mosaicLeadSpan } from './attachment-layout';
import { ComposerAttachmentsComponent } from './composer-attachments.component';
import { MessageAttachmentsComponent } from './message-attachments.component';
import { ComposerFolderCardComponent } from './composer-folder-card.component';
import type { ComposerFolder } from '../chat-input/folder-card.component';
import type { FolderAttachmentData } from '../../services/models/message.model';

const MB = 1024 * 1024;

function sizedFile(name: string, type: string, size = 2048): File {
  const file = new File(['col a,col b\n1,2\n'], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

function upload(id: string, status: PendingUpload['status'], extra: Partial<PendingUpload> = {}): PendingUpload {
  return {
    file: sizedFile(`${id}.pdf`, 'application/pdf'),
    uploadId: id,
    status,
    progress: status === 'uploading' ? 40 : 100,
    ...extra,
  };
}

function sent(id: string, mimeType = 'application/pdf', filename = `${id}.pdf`): FileAttachmentData {
  return { uploadId: id, filename, mimeType, sizeBytes: 2 * MB };
}

function restored(id: string): FileMetadata {
  return {
    uploadId: id,
    filename: `${id}.docx`,
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    sizeBytes: 4096,
    sessionId: 's1',
    status: 'ready',
    createdAt: '2026-11-10T00:00:00Z',
  };
}

const opened: unknown[] = [];
const storedPreviews = new Map<string, AttachmentPreview>();
const previewsService = { preview: vi.fn(async (id: string) => storedPreviews.get(id) ?? null) };
const fileUploadService = {
  getPreviewUrl: vi.fn().mockResolvedValue({ url: 'https://s3.example/x' }),
  getTextSnippet: vi.fn().mockResolvedValue({ snippet: '# Title\n\nBody' }),
  getThumbnail: vi.fn().mockResolvedValue({ status: 'unsupported' }),
  getThumbnailWhenRendered: vi.fn().mockResolvedValue({ status: 'unsupported' }),
};

async function configure(imports: unknown[]): Promise<void> {
  TestBed.resetTestingModule();
  opened.length = 0;
  await TestBed.configureTestingModule({
    imports: imports as never[],
    providers: [
      { provide: FEATURES, useValue: { ...environment.features, attachmentKb: false, attachmentCards: true } },
      { provide: FileUploadService, useValue: fileUploadService },
      { provide: FilePreviewStateService, useValue: { open: (ref: unknown) => opened.push(ref) } },
      { provide: AttachmentPreviewsService, useValue: previewsService },
    ],
  }).compileComponents();
}

async function card(inputs: Record<string, unknown>) {
  await configure([AttachmentCardComponent]);
  const fixture = TestBed.createComponent(AttachmentCardComponent);
  for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
  fixture.detectChanges();
  await fixture.whenStable();
  // Local previews read the File asynchronously.
  await new Promise((resolve) => setTimeout(resolve, 10));
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

/** What a sighted reader sees: screen-reader-only text left out. */
function text(el: Element | null): string {
  if (!el) return '';
  const copy = el.cloneNode(true) as Element;
  copy.querySelectorAll('.sr-only').forEach((node) => node.remove());
  return (copy.textContent ?? '').replace(/\s+/g, ' ').trim();
}

const bar = (el: HTMLElement) => el.querySelector<HTMLElement>('[data-testid="content-card-progress"] > div');

describe('mosaicLeadSpan', () => {
  it('lets the first card fill whatever the last row would leave empty', () => {
    expect(mosaicLeadSpan(1, 2)).toBe(1);
    expect(mosaicLeadSpan(3, 2)).toBe(2);
    expect(mosaicLeadSpan(4, 2)).toBe(1);
    expect(mosaicLeadSpan(5, 2)).toBe(2);
    expect(mosaicLeadSpan(4, 3)).toBe(3);
    expect(mosaicLeadSpan(5, 3)).toBe(2);
    expect(mosaicLeadSpan(5, 1)).toBe(1);
  });
});

describe('csvPreviewRows', () => {
  it('takes the first non-blank rows and columns, unquoting cells', () => {
    expect(csvPreviewRows('"a",b,c,d,e\n\n1,2\n', 6, 4)).toEqual([
      ['a', 'b', 'c', 'd'],
      ['1', '2'],
    ]);
  });
});

describe('AttachmentCardComponent', () => {
  it('shows type, progress and a cancel button while uploading in the composer', async () => {
    const el = await card({ pendingUpload: upload('report', 'uploading') });
    expect(text(el)).toContain('report.pdf');
    expect(text(el)).toContain('PDF · Uploading 40%');
    expect(bar(el)?.style.width).toBe('40%');
    expect(el.querySelector('button[aria-label="Cancel upload of report.pdf"]')).not.toBeNull();
  });

  it('shows a fetch label and no bar for a file a connected source is sending', async () => {
    const el = await card({ pendingUpload: upload('drive', 'uploading', { statusLabel: 'Adding from Google Drive…' }) });
    expect(text(el)).toContain('Adding from Google Drive…');
    expect(bar(el)).toBeNull();
  });

  it('offers Retry and Remove on a failed upload, and says why it failed', async () => {
    const el = await card({ pendingUpload: upload('report', 'error', { error: 'The file is empty.' }) });
    expect(text(el)).toContain('The file is empty.');
    expect(el.querySelector('button[aria-label="Retry upload of report.pdf"]')).not.toBeNull();
    expect(el.querySelector('button[aria-label="Remove report.pdf"]')).not.toBeNull();
  });

  it('marks a large document attached by reference, in short and in full', async () => {
    const big = upload('manual', 'ready', { file: sizedFile('manual.pdf', 'application/pdf', 38 * MB) });
    const el = await card({ pendingUpload: big });
    expect(text(el)).toContain('PDF · 38 MB · Read on demand');
    expect(el.querySelector('article')?.getAttribute('aria-label')).toBe(
      'manual.pdf, Large document, read on demand',
    );
  });

  it('previews a local text file from its own bytes before the upload finishes', async () => {
    const csv = upload('grades', 'uploading', { file: sizedFile('grades.csv', 'text/csv') });
    const el = await card({ pendingUpload: csv });
    expect(text(el.querySelector('table'))).toContain('col a');
    expect(fileUploadService.getTextSnippet).not.toHaveBeenCalledWith('grades');
  });

  it('takes a restored file and offers removal, not retry', async () => {
    const el = await card({ file: restored('notes') });
    expect(text(el)).toContain('DOCX · 4 KB');
    expect(el.querySelector('button[aria-label^="Retry"]')).toBeNull();
    expect(el.querySelector('button[aria-label="Remove notes.docx"]')).not.toBeNull();
  });

  it('in a message, opens a pane-previewable file in the docked pane', async () => {
    const docx = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    const el = await card({ attachment: sent('brief', docx, 'brief.docx'), surface: 'message' });
    const button = el.querySelector<HTMLButtonElement>('button[aria-label="Preview brief.docx"]');
    expect(button).not.toBeNull();
    expect(el.querySelector('button[aria-label^="Remove"]')).toBeNull();
    button!.click();
    expect(opened).toEqual([{ uploadId: 'brief', filename: 'brief.docx' }]);
  });
});

describe('stored previews', () => {
  const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
  const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

  it('draws a Word file\'s opening lines', async () => {
    storedPreviews.set('memo', { version: 1, kind: 'doc', lines: ['Memo to the board', 'Costs held flat'] });
    const el = await card({ attachment: sent('memo', DOCX, 'memo.docx'), surface: 'message' });
    const lines = Array.from(el.querySelectorAll('app-attachment-preview p')).map((p) => p.textContent?.trim());
    expect(lines).toEqual(['Memo to the board', 'Costs held flat']);
  });

  it('draws a workbook\'s first rows and its sheet names', async () => {
    storedPreviews.set('budget', { version: 1, kind: 'sheet', sheets: ['Budget', 'Notes'], rows: [['Line', 'FY27'], ['Travel', '1200']] });
    const el = await card({ attachment: sent('budget', XLSX, 'budget.xlsx'), surface: 'message' });
    const preview = el.querySelector('app-attachment-preview');
    const cells = Array.from(preview?.querySelectorAll('td') ?? []).map((td) => td.textContent?.trim());
    expect(cells).toEqual(['Line', 'FY27', 'Travel', '1200']);
    expect(text(preview)).toContain('Notes');
  });

  it('draws a deck\'s first slide', async () => {
    storedPreviews.set('talk', { version: 1, kind: 'deck', slides: 12, title: 'Quarterly review', bullets: ['Revenue up'] });
    const el = await card({ attachment: sent('talk', PPTX, 'talk.pptx'), surface: 'message' });
    const preview = el.querySelector('app-attachment-preview');
    expect(preview?.querySelector('p')?.textContent?.trim()).toBe('Quarterly review');
    expect(preview?.querySelector('li')?.textContent?.trim()).toBe('Revenue up');
  });

  it('keeps the skeleton for a file with no stored preview', async () => {
    const el = await card({ attachment: sent('old', DOCX, 'old.docx'), surface: 'message' });
    expect(el.querySelector('app-attachment-preview p')).toBeNull();
  });

  it('asks as a fresh upload while the composer still holds the file', async () => {
    previewsService.preview.mockClear();
    const docx = upload('new', 'ready', { file: sizedFile('new.docx', DOCX) });
    await card({ pendingUpload: docx });
    expect(previewsService.preview).toHaveBeenCalledWith('new', { fresh: true });
  });
});

describe('MessageAttachmentsComponent', () => {
  async function mount(attachments: FileAttachmentData[]) {
    await configure([MessageAttachmentsComponent]);
    const fixture = TestBed.createComponent(MessageAttachmentsComponent);
    fixture.componentRef.setInput('attachments', attachments);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture;
  }

  const ids = (n: number) => Array.from({ length: n }, (_, i) => sent(`f${i + 1}`));

  it('draws one card without a list, wide enough for its name and actions', async () => {
    const el = (await mount(ids(1))).nativeElement as HTMLElement;
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(1);
    expect(el.querySelector('ul')).toBeNull();
    expect(el.querySelector('app-attachment-card')?.parentElement?.className).toContain('max-w-sm');
    expect(el.querySelector('app-attachment-card')?.className).toContain('w-full');
  });

  it('spans the first of three cards so both rows are full', async () => {
    const el = (await mount(ids(3))).nativeElement as HTMLElement;
    const items = el.querySelectorAll('li');
    expect(items).toHaveLength(3);
    expect(items[0].className).toContain('@md:col-span-2');
    expect(items[1].className).not.toContain('col-span-2');
  });

  it('folds six or more into three cards and a +N tile that expands in place', async () => {
    const fixture = await mount(ids(8));
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(3);
    const tile = el.querySelector<HTMLButtonElement>('button[aria-label="Show 5 more attachments"]');
    expect(tile?.textContent).toContain('+5');

    tile!.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(8);
    expect(el.querySelector('button[aria-label^="Show"][aria-label$="more attachments"]')).toBeNull();

    const fewer = Array.from(el.querySelectorAll('button')).find((b) => text(b) === 'Show fewer');
    fewer!.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(3);
  });

  it('at the token profile\'s twenty files, folds to +17 and expands to full rows', async () => {
    const fixture = await mount(ids(20));
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(3);
    const tile = el.querySelector<HTMLButtonElement>('button[aria-label="Show 17 more attachments"]');
    expect(tile?.textContent).toContain('+17');
    expect(el.querySelector('ul')?.getAttribute('aria-label')).toBe('20 attachments');

    tile!.click();
    fixture.detectChanges();
    const items = el.querySelectorAll('li');
    expect(items).toHaveLength(20);
    // Twenty is even: no lead card spans, so ten full rows of two.
    expect(items[0].className).not.toContain('col-span-2');
  });
});

describe('ComposerAttachmentsComponent', () => {
  async function mount(inputs: { restored?: FileMetadata[]; importing?: PendingUpload[]; pending?: PendingUpload[] }) {
    await configure([ComposerAttachmentsComponent]);
    const fixture = TestBed.createComponent(ComposerAttachmentsComponent);
    for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture;
  }

  it('keeps up to five files on one line, restored first', async () => {
    const el = (await mount({ restored: [restored('r1')], pending: [upload('p1', 'ready'), upload('p2', 'uploading')] }))
      .nativeElement as HTMLElement;
    const names = Array.from(el.querySelectorAll('app-attachment-card')).map((c) => c.querySelector('p')?.textContent?.trim());
    expect(names).toEqual(['r1.docx', 'p1.pdf', 'p2.pdf']);
    expect(el.textContent).not.toContain('Show all');
  });

  it('past five, folds into a summary that keeps failures and in-flight files in view', async () => {
    const pending = [
      ...Array.from({ length: 5 }, (_, i) => upload(`ok${i}`, 'ready')),
      upload('busy', 'uploading'),
      upload('bad', 'error'),
    ];
    const fixture = await mount({ pending });
    const el = fixture.nativeElement as HTMLElement;
    expect(text(el)).toContain('7 files · 5 ready · 1 uploading · 1 failed');
    const names = Array.from(el.querySelectorAll('app-attachment-card')).map((c) => c.querySelector('p')?.textContent?.trim());
    expect(names).toEqual(['bad.pdf', 'busy.pdf']);

    const toggle = Array.from(el.querySelectorAll('button')).find((b) => text(b) === 'Show all')!;
    toggle.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(7);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
  });

  it('summarises twenty files and shows them all on request', async () => {
    const pending = [
      ...Array.from({ length: 12 }, (_, i) => upload(`doc${i}`, 'ready')),
      ...Array.from({ length: 8 }, (_, i) => upload(`img${i}`, 'ready', { file: sizedFile(`img${i}.jpg`, 'image/jpeg') })),
    ];
    const fixture = await mount({ pending });
    const el = fixture.nativeElement as HTMLElement;
    expect(text(el)).toContain('20 files · 20 ready');
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(0);

    Array.from(el.querySelectorAll('button')).find((b) => text(b) === 'Show all')!.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(20);
  });
});

describe('images and folders as cards', () => {
  const folder: FolderAttachmentData = {
    folderAttachId: 'fa-1',
    name: 'Grant drafts',
    source: 'Google Drive',
    status: 'ready',
    fileCount: 9,
    files: ['budget/Budget.xlsx', 'Narrative.docx', 'Letters/Support.pdf'],
    moreCount: 6,
  };

  async function message(attachments: FileAttachmentData[], folders: FolderAttachmentData[] = []) {
    await configure([MessageAttachmentsComponent]);
    const fixture = TestBed.createComponent(MessageAttachmentsComponent);
    fixture.componentRef.setInput('attachments', attachments);
    fixture.componentRef.setInput('folders', folders);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('lays out documents, images and folders in one mosaic', async () => {
    const el = (await message([sent('brief'), sent('photo', 'image/png', 'photo.png')], [folder]))
      .nativeElement as HTMLElement;
    expect(el.querySelectorAll('ul[aria-label] > li')).toHaveLength(3);
    expect(el.querySelector('ul')?.getAttribute('aria-label')).toBe('3 attachments');
    expect(el.querySelector('app-folder-attachment-card')).not.toBeNull();
  });

  it('opens the message images in the lightbox at the one clicked', async () => {
    const fixture = await message([sent('a', 'image/png', 'a.png'), sent('doc'), sent('b', 'image/jpeg', 'b.jpg')]);
    const el = fixture.nativeElement as HTMLElement;
    el.querySelector<HTMLButtonElement>('button[aria-label="Preview b.jpg"]')!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
    const lightbox = fixture.debugElement.query((node) => node.name === 'app-image-lightbox');
    expect(lightbox).not.toBeNull();
    expect(lightbox.componentInstance.images().map((i: { filename: string }) => i.filename)).toEqual(['a.png', 'b.jpg']);
    expect(lightbox.componentInstance.startIndex()).toBe(1);
  });

  it('shows a folder with its first files, and its whole list on Files', async () => {
    const fixture = await message([], [folder]);
    const el = fixture.nativeElement as HTMLElement;
    expect(text(el)).toContain('Folder · 9 files · Google Drive');
    expect(el.querySelector('app-folder-preview')?.textContent).toContain('Budget.xlsx');
    const files = el.querySelector<HTMLButtonElement>('button[aria-expanded="false"]')!;
    files.click();
    fixture.detectChanges();
    expect(files.getAttribute('aria-expanded')).toBe('true');
    expect(el.querySelector(`#${files.getAttribute('aria-controls')}`)?.textContent).toContain('Letters/Support.pdf');
  });

  async function composerFolder(overrides: Partial<ComposerFolder>) {
    await configure([ComposerFolderCardComponent]);
    const fixture = TestBed.createComponent(ComposerFolderCardComponent);
    fixture.componentRef.setInput('folder', {
      localId: 'l1', folderAttachId: 'fa-1', sessionId: 's1', name: 'Grant drafts', source: 'Google Drive',
      row: null, phase: 'attaching', error: null, ...overrides,
    });
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('draws the composer folder while it is being added, with Remove', async () => {
    const el = await composerFolder({});
    expect(text(el)).toContain('Adding from Google Drive…');
    expect(el.querySelector('button[aria-label="Remove folder Grant drafts"]')).not.toBeNull();
  });

  it('offers Connect when the source needs consent again', async () => {
    const el = await composerFolder({
      phase: 'attached',
      row: { status: 'partial', error: 'needs_consent', counts: { attached: 2 }, files: [] } as never,
    });
    expect(Array.from(el.querySelectorAll('button')).some((b) => b.textContent?.includes('Connect'))).toBe(true);
  });
});

