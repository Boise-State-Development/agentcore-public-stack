import { describe, it, expect } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { FileCardComponent } from './file-card.component';
import {
  AttachmentSearchStatusService,
  type AttachmentSearchStatus,
  type PendingUpload,
} from '../../services/file-upload';
import { FEATURES } from '../../services/features';
import { environment } from '../../../environments/environment';

/**
 * The composer's attachment card says when a file is attached by reference
 * (docs/specs/large-document-attachments.md §4G): a document over the inline
 * limit is read on demand, not sent whole, from the moment it is uploaded.
 */
describe('FileCardComponent large document chip', () => {
  const MB = 1024 * 1024;

  function sizedFile(name: string, type: string, size: number): File {
    const file = new File(['x'], name, { type });
    Object.defineProperty(file, 'size', { value: size });
    return file;
  }

  async function mount(upload: PendingUpload) {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({ imports: [FileCardComponent] }).compileComponents();
    const fixture = TestBed.createComponent(FileCardComponent);
    fixture.componentRef.setInput('pendingUpload', upload);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture;
  }

  const chip = (fixture: { nativeElement: HTMLElement }) =>
    fixture.nativeElement.querySelector('[data-testid="reference-chip"]') as HTMLElement | null;

  it('shows read on demand, with the full copy for assistive tech, once a large document is uploaded', async () => {
    const fixture = await mount({
      file: sizedFile('policy-manual.pdf', 'application/pdf', 38 * MB),
      uploadId: 'up-1',
      status: 'ready',
      progress: 100,
    });

    const el = chip(fixture);
    expect(el?.textContent).toContain('38 MB');
    expect(el?.textContent).toContain('Read on demand');
    expect(el?.querySelector('.sr-only')?.textContent).toBe('Large document, read on demand');
  });

  it('classifies by extension when the browser reports no MIME type', async () => {
    const fixture = await mount({
      file: sizedFile('policy-manual.pdf', '', 38 * MB),
      uploadId: 'up-1',
      status: 'ready',
      progress: 100,
    });

    expect(chip(fixture)).not.toBeNull();
  });

  it('shows upload progress, not the chip, while the file is still uploading', async () => {
    const fixture = await mount({
      file: sizedFile('policy-manual.pdf', 'application/pdf', 38 * MB),
      uploadId: 'up-1',
      status: 'uploading',
      progress: 40,
    });

    expect(chip(fixture)).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Uploading... 40%');
  });

  it('leaves inline-size documents and large spreadsheets unmarked', async () => {
    const small = await mount({
      file: sizedFile('memo.pdf', 'application/pdf', 2 * MB),
      uploadId: 'up-2',
      status: 'ready',
      progress: 100,
    });
    expect(chip(small)).toBeNull();

    const sheet = await mount({
      file: sizedFile('data.csv', 'text/csv', 10 * MB),
      uploadId: 'up-3',
      status: 'ready',
      progress: 100,
    });
    expect(chip(sheet)).toBeNull();
  });
});

/**
 * With the attachments KB the composer card's short chip follows the search
 * index too, sharing the message card's poll.
 */
describe('FileCardComponent search chip', () => {
  const MB = 1024 * 1024;

  class FakeSearchStatus {
    readonly status = signal<AttachmentSearchStatus | null>(null);
    readonly tracked: string[] = [];
    statusFor(): AttachmentSearchStatus | null {
      return this.status();
    }
    track(id: string): void {
      this.tracked.push(id);
    }
  }

  async function mount(status: PendingUpload['status']) {
    const fake = new FakeSearchStatus();
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [FileCardComponent],
      providers: [
        { provide: FEATURES, useValue: { ...environment.features, attachmentKb: true } },
        { provide: AttachmentSearchStatusService, useValue: fake },
      ],
    }).compileComponents();
    const file = new File(['x'], 'policy-manual.pdf', { type: 'application/pdf' });
    Object.defineProperty(file, 'size', { value: 38 * MB });
    const fixture = TestBed.createComponent(FileCardComponent);
    fixture.componentRef.setInput('pendingUpload', { file, uploadId: 'up-1', status, progress: 100 });
    fixture.detectChanges();
    await fixture.whenStable();
    return { fixture, fake };
  }

  const chip = (fixture: { nativeElement: HTMLElement }) =>
    fixture.nativeElement.querySelector('[data-testid="reference-chip"]') as HTMLElement | null;

  it('shows the short form, with the full copy for assistive tech, as indexing gives way to searchable', async () => {
    const { fixture, fake } = await mount('ready');
    expect(fake.tracked).toEqual(['up-1']);

    fake.status.set('indexing');
    fixture.detectChanges();
    expect(chip(fixture)?.textContent).toContain('Indexing…');
    expect(chip(fixture)?.querySelector('.sr-only')?.textContent).toBe('Large document, indexing for search…');

    fake.status.set('searchable');
    fixture.detectChanges();
    expect(chip(fixture)?.textContent).toContain('38 MB');
    expect(chip(fixture)?.textContent).toContain('Searchable');
    expect(chip(fixture)?.querySelector('.sr-only')?.textContent).toBe('Large document, searchable');
  });

  it('does not poll before the upload is complete', async () => {
    const { fake } = await mount('completing');
    expect(fake.tracked).toEqual([]);
  });
});
