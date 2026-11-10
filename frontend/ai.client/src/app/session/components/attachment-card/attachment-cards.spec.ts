import { describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';

import { environment } from '../../../../environments/environment';
import { FEATURES } from '../../../services/features';
import { FileUploadService, type FileMetadata, type PendingUpload } from '../../../services/file-upload';
import { FilePreviewStateService } from '../../services/file-preview/file-preview-state.service';
import type { FileAttachmentData } from '../../services/models/message.model';
import { AttachmentCardComponent } from './attachment-card.component';
import { csvPreviewRows } from './attachment-kind';
import { mosaicLeadSpan } from './attachment-layout';
import { ComposerAttachmentsComponent } from './composer-attachments.component';
import { MessageAttachmentsComponent } from './message-attachments.component';

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
    s3Uri: 's3://b/k',
    status: 'ready',
    createdAt: '2026-11-10T00:00:00Z',
  };
}

const opened: unknown[] = [];
const fileUploadService = {
  getPreviewUrl: vi.fn().mockResolvedValue({ url: 'https://s3.example/x' }),
  getTextSnippet: vi.fn().mockResolvedValue({ snippet: '# Title\n\nBody' }),
  getThumbnail: vi.fn().mockResolvedValue({ status: 'unsupported' }),
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

  it('draws one card without a list', async () => {
    const el = (await mount(ids(1))).nativeElement as HTMLElement;
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(1);
    expect(el.querySelector('ul')).toBeNull();
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
    const tile = el.querySelector<HTMLButtonElement>('button[aria-label="Show 5 more files"]');
    expect(tile?.textContent).toContain('+5');

    tile!.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(8);
    expect(el.querySelector('button[aria-label^="Show"][aria-label$="more files"]')).toBeNull();

    const fewer = Array.from(el.querySelectorAll('button')).find((b) => text(b) === 'Show fewer');
    fewer!.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-attachment-card')).toHaveLength(3);
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
});
