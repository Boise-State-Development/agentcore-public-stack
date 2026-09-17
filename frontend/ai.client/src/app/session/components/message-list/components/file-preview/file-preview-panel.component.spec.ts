import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { FilePreviewPanelComponent } from './file-preview-panel.component';
import { DocxViewerComponent } from './docx-viewer.component';
import { FilePreviewStateService } from '../../../../services/file-preview/file-preview-state.service';
import {
  FilePreviewError,
  FilePreviewHttpService,
} from '../../../../services/file-preview/file-preview-http.service';
import { DOCX_MIME } from '../../../../services/file-preview/file-preview.model';
import { ConfigService } from '../../../../../services/config.service';

const renderAsync = vi.fn();

vi.mock('docx-preview', () => ({
  renderAsync: (...args: unknown[]) => renderAsync(...args),
}));

describe('FilePreviewPanelComponent', () => {
  let fixture: ComponentFixture<FilePreviewPanelComponent>;
  let state: FilePreviewStateService;
  let fetchDocument: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    renderAsync.mockReset();
    // The component memoizes its dynamic import('docx-preview') on a static
    // field, and the builder runs vitest with isolate: false, so a sibling spec
    // that rendered first would pin *its* mocked module for this file too.
    (DocxViewerComponent as unknown as { libraryPromise: unknown }).libraryPromise = null;
    renderAsync.mockImplementation((_data, host: HTMLElement) => {
      host.appendChild(document.createElement('section'));
      return Promise.resolve();
    });

    fetchDocument = vi.fn().mockResolvedValue({
      bytes: new ArrayBuffer(16),
      mimeType: DOCX_MIME,
      filename: 'plan.docx',
    });

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [FilePreviewPanelComponent],
      providers: [
        { provide: FilePreviewHttpService, useValue: { fetchDocument } },
      ],
    }).compileComponents();

    TestBed.inject(ConfigService).appApiUrl.set('/api');
    state = TestBed.inject(FilePreviewStateService);
    fixture = TestBed.createComponent(FilePreviewPanelComponent);
    fixture.detectChanges();
  });

  /** Drain the promise chain (fetch -> dynamic import -> renderAsync)
   *  and repaint. Matches the `flush` helper in the artifact panel's
   *  spec — `whenStable` alone does not await bare promises here. */
  async function settle(): Promise<void> {
    for (let i = 0; i < 20; i++) await Promise.resolve();
    fixture.detectChanges();
    for (let i = 0; i < 20; i++) await Promise.resolve();
    fixture.detectChanges();
  }

  async function openPreview(): Promise<void> {
    state.open({ uploadId: 'up1', filename: 'plan.docx' });
    fixture.detectChanges();
    await settle();
  }

  it('renders nothing while closed', () => {
    expect(fixture.nativeElement.querySelector('aside')).toBeNull();
    expect(fetchDocument).not.toHaveBeenCalled();
  });

  it('opens on the named file and fetches it once', async () => {
    await openPreview();

    const aside = fixture.nativeElement.querySelector('aside');
    expect(aside).not.toBeNull();
    expect(aside.getAttribute('aria-label')).toBe(
      'File preview: plan.docx',
    );
    expect(fixture.nativeElement.textContent).toContain('plan.docx');
    expect(fetchDocument).toHaveBeenCalledExactlyOnceWith('up1');
  });

  it('offers the durable download route for the open file', async () => {
    await openPreview();

    const anchor = fixture.nativeElement.querySelector('a');
    expect(anchor?.getAttribute('href')).toBe('/api/files/up1/download');
    expect(anchor?.getAttribute('download')).toBe('plan.docx');
  });

  it('clears the loading state once the document paints', async () => {
    await openPreview();

    expect(fixture.nativeElement.textContent).not.toContain('Loading preview');
  });

  it('shows a retry affordance for a retryable failure', async () => {
    fetchDocument.mockRejectedValue(
      new FilePreviewError('This document is no longer available.', true),
    );

    await openPreview();

    expect(fixture.nativeElement.textContent).toContain(
      'This document is no longer available.',
    );
    const retry = fixture.nativeElement.querySelector('[role="alert"] button');
    expect(retry?.textContent).toContain('Try again');
  });

  it('refetches when retry is pressed', async () => {
    fetchDocument.mockRejectedValueOnce(
      new FilePreviewError('This document is no longer available.', true),
    );

    await openPreview();
    fixture.nativeElement
      .querySelector('[role="alert"] button')
      .dispatchEvent(new Event('click'));
    await settle();

    expect(fetchDocument).toHaveBeenCalledTimes(2);
    expect(fixture.nativeElement.textContent).not.toContain(
      'This document is no longer available.',
    );
  });

  it('offers no retry for a failure that cannot succeed twice', async () => {
    fetchDocument.mockRejectedValue(
      new FilePreviewError("This file is a application/pdf, which can't be previewed here.", false),
    );

    await openPreview();

    expect(fixture.nativeElement.textContent).toContain("can't be previewed here");
    expect(fixture.nativeElement.textContent).not.toContain('Try again');
  });

  it('keeps download reachable when the document cannot be rendered', async () => {
    // A user whose preview failed still wants to open the file in Word.
    renderAsync.mockRejectedValue(new Error('corrupt zip'));

    await openPreview();

    expect(fixture.nativeElement.textContent).toContain(
      "couldn't be read as a Word document",
    );
    expect(
      fixture.nativeElement.querySelector('a')?.getAttribute('href'),
    ).toBe('/api/files/up1/download');
  });

  it('closes on the close button', async () => {
    await openPreview();

    const close = fixture.nativeElement.querySelector(
      'button[aria-label="Close document preview"]',
    );
    close.dispatchEvent(new Event('click'));
    fixture.detectChanges();

    expect(state.openFile()).toBeNull();
    expect(fixture.nativeElement.querySelector('aside')).toBeNull();
  });

  it('refetches when the pane switches to another file', async () => {
    await openPreview();

    state.open({ uploadId: 'up2', filename: 'notes.docx' });
    fixture.detectChanges();
    await settle();

    expect(fetchDocument).toHaveBeenLastCalledWith('up2');
    expect(fixture.nativeElement.textContent).toContain('notes.docx');
  });
});
