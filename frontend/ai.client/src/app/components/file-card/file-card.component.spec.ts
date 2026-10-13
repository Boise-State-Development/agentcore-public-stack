import { describe, it, expect } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { FileCardComponent } from './file-card.component';
import type { PendingUpload } from '../../services/file-upload';

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
