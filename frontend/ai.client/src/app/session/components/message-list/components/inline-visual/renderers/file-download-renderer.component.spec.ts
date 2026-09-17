import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { ConfigService } from '../../../../../../services/config.service';
import { FilePreviewStateService } from '../../../../../services/file-preview/file-preview-state.service';
import { FileDownloadRendererComponent } from './file-download-renderer.component';

describe('FileDownloadRendererComponent', () => {
  let fixture: ComponentFixture<FileDownloadRendererComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FileDownloadRendererComponent],
    }).compileComponents();

    TestBed.inject(ConfigService).appApiUrl.set('/api');
    fixture = TestBed.createComponent(FileDownloadRendererComponent);
  });

  function render(payload: unknown): HTMLAnchorElement | null {
    fixture.componentRef.setInput('payload', payload);
    fixture.detectChanges();
    return fixture.nativeElement.querySelector('a');
  }

  function previewButton(): HTMLButtonElement | null {
    return fixture.nativeElement.querySelector('button');
  }

  it('links an upload_id payload at the durable download route', () => {
    const anchor = render({
      filename: 'plan.docx',
      upload_id: '1a098460e41_ba6e8f342f1744e2',
      size_kb: '51.0 KB',
    });

    expect(anchor?.getAttribute('href')).toBe(
      '/api/files/1a098460e41_ba6e8f342f1744e2/download',
    );
    expect(fixture.nativeElement.textContent).toContain('plan.docx');
    expect(fixture.nativeElement.textContent).toContain('51.0 KB');
  });

  it('routes a legacy presigned download_url through the same durable route', () => {
    // Cards persisted before the backend stopped emitting signed URLs carry a
    // download_url whose signature expired an hour after it was written.
    const anchor = render({
      filename: 'plan.docx',
      download_url:
        'https://bucket.s3.us-west-2.amazonaws.com/user-files/u1/s1/up9/plan.docx' +
        '?X-Amz-Signature=deadbeef',
    });

    expect(anchor?.getAttribute('href')).toBe('/api/files/up9/download');
  });

  it('renders nothing when neither an upload id nor a usable URL is present', () => {
    expect(render({ filename: 'plan.docx' })).toBeNull();
    expect(render({ upload_id: 'up1' })).toBeNull();
    expect(render(null)).toBeNull();
  });

  it('offers a preview for a .docx', () => {
    render({ filename: 'plan.docx', upload_id: 'up1' });

    expect(previewButton()?.textContent).toContain('Preview');
  });

  it('opens the docked pane on the file when preview is pressed', () => {
    render({ filename: 'plan.docx', upload_id: 'up1' });
    previewButton()?.dispatchEvent(new Event('click'));

    expect(TestBed.inject(FilePreviewStateService).openFile()).toEqual({
      uploadId: 'up1',
      filename: 'plan.docx',
    });
  });

  it('offers a preview for a legacy card once the upload id is recovered', () => {
    render({
      filename: 'plan.docx',
      download_url:
        'https://bucket.s3.us-west-2.amazonaws.com/user-files/u1/s1/up9/plan.docx' +
        '?X-Amz-Signature=deadbeef',
    });
    previewButton()?.dispatchEvent(new Event('click'));

    expect(TestBed.inject(FilePreviewStateService).openFile()).toEqual({
      uploadId: 'up9',
      filename: 'plan.docx',
    });
  });

  it.each([
    ['deck.pptx'],
    ['rows.csv'],
    ['budget.xlsx'],
  ])('offers a preview for %s as well as a .docx', (filename) => {
    expect(render({ filename, upload_id: 'up2' })).not.toBeNull();
    expect(previewButton()).not.toBeNull();
  });

  it('offers no preview for formats the pane cannot render', () => {
    // The legacy binary formats are not OOXML at all, and .xls would
    // need a library we did not take on for a format nothing in the
    // product generates. All of them still get their download link;
    // only the button is withheld.
    for (const filename of ['old.doc', 'old.ppt', 'old.xls', 'notes.txt']) {
      expect(render({ filename, upload_id: 'up1' })).not.toBeNull();
      expect(previewButton()).toBeNull();
    }
  });
});
