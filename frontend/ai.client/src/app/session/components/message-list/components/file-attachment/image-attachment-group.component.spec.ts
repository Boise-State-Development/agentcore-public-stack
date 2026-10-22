import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { ImageAttachmentGroupComponent } from './image-attachment-group.component';
import { FileUploadService, PreviewUrlResponse } from '../../../../../services/file-upload';
import { FileAttachmentData } from '../../../../services/models/message.model';

const ATTACHMENT: FileAttachmentData = {
  uploadId: 'upload-1',
  filename: 'screenshot.png',
  mimeType: 'image/png',
  sizeBytes: 1024,
};

function previewResponse(url: string, expiresInMs = 10 * 60 * 1000): PreviewUrlResponse {
  return {
    uploadId: ATTACHMENT.uploadId,
    url,
    expiresAt: new Date(Date.now() + expiresInMs).toISOString(),
    mimeType: ATTACHMENT.mimeType,
    filename: ATTACHMENT.filename,
  };
}

/** Let the constructor's queueMicrotask and the awaited fetch chain settle. */
async function settle(fixture: ComponentFixture<ImageAttachmentGroupComponent>): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  fixture.detectChanges();
}

/**
 * Preview URLs are presigned for ~10 minutes and the tiles are `loading="lazy"`,
 * so an image scrolled back into view later in a long conversation asks S3 with
 * a dead signature. Before this, nothing listened for the `<img>` error and the
 * browser's broken-image glyph sat in the message with the filename as alt text.
 */
describe('ImageAttachmentGroupComponent expired preview URLs', () => {
  let fixture: ComponentFixture<ImageAttachmentGroupComponent>;
  let getPreviewUrl: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    getPreviewUrl = vi.fn();

    await TestBed.configureTestingModule({
      imports: [ImageAttachmentGroupComponent],
      providers: [{ provide: FileUploadService, useValue: { getPreviewUrl } }],
    }).compileComponents();

  });

  /**
   * Build after the mock is primed — the component fetches from its
   * constructor, so creating it in `beforeEach` would burn the first
   * `mockResolvedValueOnce` on a bare `vi.fn()`.
   */
  async function create(): Promise<void> {
    fixture = TestBed.createComponent(ImageAttachmentGroupComponent);
    fixture.componentRef.setInput('attachments', [ATTACHMENT]);
    fixture.detectChanges();
    await settle(fixture);
  }

  function img(): HTMLImageElement | null {
    return fixture.nativeElement.querySelector('img');
  }

  it('re-mints a fresh URL when the tile fails to load', async () => {
    getPreviewUrl
      .mockResolvedValueOnce(previewResponse('https://s3/expired'))
      .mockResolvedValueOnce(previewResponse('https://s3/fresh'));

    await create();
    expect(img()?.getAttribute('src')).toBe('https://s3/expired');

    img()?.dispatchEvent(new Event('error'));
    await settle(fixture);

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(img()?.getAttribute('src')).toBe('https://s3/fresh');
    expect(fixture.nativeElement.textContent).not.toContain('Preview unavailable');
  });

  it('gives up after one retry so a dead object cannot loop', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3/gone'));

    await create();

    img()?.dispatchEvent(new Event('error'));
    await settle(fixture);
    img()?.dispatchEvent(new Event('error'));
    await settle(fixture);

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(img()).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Preview unavailable');
  });

  it('restores the retry budget once an image actually loads', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3/ok'));

    await create();

    img()?.dispatchEvent(new Event('error'));
    await settle(fixture);
    // The re-minted URL rendered, so a later expiry earns its own retry.
    img()?.dispatchEvent(new Event('load'));
    await settle(fixture);
    img()?.dispatchEvent(new Event('error'));
    await settle(fixture);

    expect(getPreviewUrl).toHaveBeenCalledTimes(3);
    expect(fixture.nativeElement.textContent).not.toContain('Preview unavailable');
  });

  it('refreshes a stale URL before opening the lightbox', async () => {
    getPreviewUrl
      .mockResolvedValueOnce(previewResponse('https://s3/stale', -1000))
      .mockResolvedValueOnce(previewResponse('https://s3/fresh'));

    await create();

    fixture.nativeElement.querySelector('button')?.click();
    await settle(fixture);

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(fixture.nativeElement.querySelector('app-image-lightbox')).not.toBeNull();
  });

  it('leaves a still-valid URL alone when the lightbox opens', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3/fresh'));

    await create();

    fixture.nativeElement.querySelector('button')?.click();
    await settle(fixture);

    expect(getPreviewUrl).toHaveBeenCalledTimes(1);
  });
});
