import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { FileUploadService, type PreviewUrlResponse } from '../../../../../../services/file-upload';
import { ImageOutputCardComponent } from './image-output-card.component';

function previewResponse(url: string, expiresInMs = 10 * 60 * 1000): PreviewUrlResponse {
  return {
    uploadId: 'up1',
    url,
    expiresAt: new Date(Date.now() + expiresInMs).toISOString(),
    mimeType: 'image/jpeg',
    filename: 'thumbs.jpg',
  };
}

describe('ImageOutputCardComponent', () => {
  let fixture: ComponentFixture<ImageOutputCardComponent>;
  let getPreviewUrl: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    getPreviewUrl = vi.fn();
    await TestBed.configureTestingModule({
      imports: [ImageOutputCardComponent],
      providers: [{ provide: FileUploadService, useValue: { getPreviewUrl } }],
    }).compileComponents();
  });

  /** Let the awaited mint settle, then draw what it set. */
  async function settle(): Promise<void> {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
  }

  /** Build after the mock is primed: the card mints as soon as it is drawn. */
  async function create(): Promise<HTMLElement> {
    fixture = TestBed.createComponent(ImageOutputCardComponent);
    fixture.componentRef.setInput('uploadId', 'up1');
    fixture.componentRef.setInput('filename', 'thumbs.jpg');
    fixture.componentRef.setInput('href', '/api/files/up1/download');
    fixture.componentRef.setInput('meta', 'JPG · 53.7 KB');
    fixture.detectChanges();
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  const image = (el: HTMLElement) => el.querySelector<HTMLImageElement>('app-content-card img');
  const expandButton = (el: HTMLElement) => el.querySelector<HTMLButtonElement>('app-content-card button');
  const lightbox = (el: HTMLElement) => el.querySelector('app-image-lightbox');

  it('shows the whole image in the tray, fitted rather than cropped', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3.example/a'));
    const el = await create();

    expect(getPreviewUrl).toHaveBeenCalledWith('up1');
    expect(image(el)?.getAttribute('src')).toBe('https://s3.example/a');
    expect(image(el)?.className).toContain('object-contain');
    // Upright at every width, the tray as tall as the image.
    expect(el.querySelector('article')?.className).not.toContain('flex-row');
    expect(el.textContent).toContain('thumbs.jpg');
    expect(el.textContent).toContain('JPG · 53.7 KB');
  });

  it('keeps Download on the durable route', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3.example/a'));
    const el = await create();

    const link = el.querySelector('a');
    expect(link?.getAttribute('href')).toBe('/api/files/up1/download');
    expect(link?.getAttribute('download')).toBe('thumbs.jpg');
    expect(link?.getAttribute('aria-label')).toBe('Download thumbs.jpg');
  });

  it('opens the image in the lightbox when it is pressed', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3.example/a'));
    const el = await create();

    expect(lightbox(el)).toBeNull();
    expect(expandButton(el)?.getAttribute('aria-label')).toBe('Expand thumbs.jpg');
    expandButton(el)?.click();
    fixture.detectChanges();

    const dialog = el.querySelector('app-image-lightbox [role="dialog"]');
    expect(dialog?.getAttribute('aria-label')).toBe('Image preview: thumbs.jpg');
    expect(dialog?.querySelector('img')?.getAttribute('src')).toBe('https://s3.example/a');
    expect(getPreviewUrl).toHaveBeenCalledTimes(1);
  });

  it('closes the lightbox on Escape', async () => {
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3.example/a'));
    const el = await create();
    expandButton(el)?.click();
    fixture.detectChanges();

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    fixture.detectChanges();

    expect(lightbox(el)).toBeNull();
  });

  it('mints a fresh URL before opening the lightbox on one about to expire', async () => {
    getPreviewUrl
      .mockResolvedValueOnce(previewResponse('https://s3.example/old', 5_000))
      .mockResolvedValueOnce(previewResponse('https://s3.example/new'));
    const el = await create();

    expandButton(el)?.click();
    await settle();

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(el.querySelector('app-image-lightbox img')?.getAttribute('src')).toBe('https://s3.example/new');
  });

  it('mints once more when the image fails to load, as an expired signature does', async () => {
    getPreviewUrl
      .mockResolvedValueOnce(previewResponse('https://s3.example/dead'))
      .mockResolvedValueOnce(previewResponse('https://s3.example/fresh'));
    const el = await create();

    image(el)?.dispatchEvent(new Event('error'));
    await settle();

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(image(el)?.getAttribute('src')).toBe('https://s3.example/fresh');
  });

  it('says the preview is unavailable after a second failure, and still offers Download', async () => {
    getPreviewUrl
      .mockResolvedValueOnce(previewResponse('https://s3.example/dead'))
      .mockResolvedValueOnce(previewResponse('https://s3.example/also-dead'));
    const el = await create();

    image(el)?.dispatchEvent(new Event('error'));
    await settle();
    image(el)?.dispatchEvent(new Event('error'));
    await settle();

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(image(el)).toBeNull();
    expect(expandButton(el)).toBeNull();
    expect(el.querySelector('[data-testid="image-output-unavailable"]')).not.toBeNull();
    expect(el.textContent).toContain('Preview unavailable');
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/api/files/up1/download');
  });

  it('gives up at once when the retry is handed the URL that just failed', async () => {
    // Setting the same URL again would not reload the image, so no second
    // error would arrive and the card would sit on a broken image.
    getPreviewUrl.mockResolvedValue(previewResponse('https://s3.example/dead'));
    const el = await create();

    image(el)?.dispatchEvent(new Event('error'));
    await settle();

    expect(getPreviewUrl).toHaveBeenCalledTimes(2);
    expect(image(el)).toBeNull();
    expect(el.textContent).toContain('Preview unavailable');
  });

  it('earns the retry back once an image has loaded', async () => {
    getPreviewUrl
      .mockResolvedValueOnce(previewResponse('https://s3.example/one'))
      .mockResolvedValueOnce(previewResponse('https://s3.example/two'))
      .mockResolvedValueOnce(previewResponse('https://s3.example/three'));
    const el = await create();

    image(el)?.dispatchEvent(new Event('error'));
    await settle();
    image(el)?.dispatchEvent(new Event('load'));
    image(el)?.dispatchEvent(new Event('error'));
    await settle();

    expect(image(el)?.getAttribute('src')).toBe('https://s3.example/three');
  });

  it('says the preview is unavailable when no URL can be minted', async () => {
    getPreviewUrl.mockRejectedValue(new Error('gone'));
    const el = await create();

    expect(image(el)).toBeNull();
    expect(el.querySelector('[data-testid="image-output-loading"]')).toBeNull();
    expect(el.textContent).toContain('Preview unavailable');
  });

  it('holds the tray open and announces the load while the URL is minted', async () => {
    getPreviewUrl.mockReturnValue(new Promise(() => undefined));
    const el = await create();

    expect(el.querySelector('[data-testid="image-output-loading"]')).not.toBeNull();
    expect(el.querySelector('[role="status"]')?.textContent).toContain('Loading thumbs.jpg');
  });
});
