import { describe, it, expect, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import {
  FILE_TYPE_STYLES,
  DEFAULT_STYLE,
  FileAttachmentBadgeComponent,
} from './file-attachment-badge.component';
import {
  ALLOWED_MIME_TYPES,
  FileUploadService,
} from '../../../../../services/file-upload';
import { FilePreviewStateService } from '../../../../services/file-preview/file-preview-state.service';

/**
 * Every uploadable type needs its own card style.
 *
 * A type missing from FILE_TYPE_STYLES doesn't fail loudly — it silently
 * falls through to DEFAULT_STYLE and the card renders a generic grey "FILE"
 * chip. That is exactly how .pptx shipped: the upload allowlist gained the
 * type but this map didn't, so decks arrived looking like anonymous blobs.
 *
 * Pinning the map against the upload allowlist means the next type added to
 * one has to be added to the other.
 */
describe('file attachment card styles', () => {
  const PPTX_MIME =
    'application/vnd.openxmlformats-officedocument.presentationml.presentation';

  it('gives .pptx its own style rather than the generic fallback', () => {
    const style = FILE_TYPE_STYLES[PPTX_MIME];
    expect(style).toBeDefined();
    expect(style.label).toBe('PPTX');
    expect(style.label).not.toBe(DEFAULT_STYLE.label);
  });

  it('uses a presentation icon for .pptx, not a plain document', () => {
    expect(FILE_TYPE_STYLES[PPTX_MIME].icon).toBe('heroPresentationChartBar');
    expect(FILE_TYPE_STYLES[PPTX_MIME].icon).not.toBe(DEFAULT_STYLE.icon);
  });

  it('styles every mime type the upload allowlist accepts', () => {
    const unstyled = Object.keys(ALLOWED_MIME_TYPES).filter(
      (mime) => !(mime in FILE_TYPE_STYLES),
    );
    expect(unstyled).toEqual([]);
  });
});

/**
 * Where a click on an attachment card goes.
 *
 * An uploaded `.docx`/`.pptx` used to fall through to the presigned-URL
 * branch, which hands the browser an OOXML file it cannot render — so
 * "open" silently became "download". The docked pane already renders both,
 * and it only ever appeared on the *generated*-file download card, so an
 * uploaded deck and a generated one behaved differently for no reason the
 * user could see.
 */
describe('FileAttachmentBadgeComponent click routing', () => {
  const PPTX_MIME =
    'application/vnd.openxmlformats-officedocument.presentationml.presentation';
  const PDF_MIME = 'application/pdf';

  async function mount(filename: string, mimeType: string) {
    const opened: unknown[] = [];
    const windowOpen = vi.fn();
    vi.stubGlobal('open', windowOpen);

    const getPreviewUrl = vi
      .fn()
      .mockResolvedValue({ url: 'https://s3.example/x?X-Amz-Signature=abc' });

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [FileAttachmentBadgeComponent],
      providers: [
        {
          provide: FilePreviewStateService,
          useValue: { open: (ref: unknown) => opened.push(ref) },
        },
        {
          provide: FileUploadService,
          useValue: {
            getPreviewUrl,
            getTextSnippet: vi.fn().mockResolvedValue({ snippet: '' }),
            getThumbnail: vi.fn().mockResolvedValue({ status: 'error' }),
          },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(FileAttachmentBadgeComponent);
    fixture.componentRef.setInput('attachment', {
      uploadId: 'up1',
      filename,
      mimeType,
      sizeBytes: 1024,
    });
    fixture.detectChanges();
    await fixture.whenStable();
    return { fixture, opened, windowOpen, getPreviewUrl };
  }

  it('opens an uploaded .pptx in the docked pane, not a new tab', async () => {
    const { fixture, opened, windowOpen } = await mount('deck.pptx', PPTX_MIME);

    fixture.nativeElement.querySelector('button').click();
    await fixture.whenStable();

    expect(opened).toEqual([{ uploadId: 'up1', filename: 'deck.pptx' }]);
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('still opens a format the pane cannot render in a new tab', async () => {
    const { fixture, opened, getPreviewUrl } = await mount('paper.pdf', PDF_MIME);

    fixture.nativeElement.querySelector('button').click();
    await fixture.whenStable();

    expect(opened).toEqual([]);
    expect(getPreviewUrl).toHaveBeenCalledWith('up1');
  });

  it('advertises Preview on the card at rest, not only on hover', async () => {
    // The card is the whole affordance — there is no separate button — so a
    // hover-only hint tells a reader of the thread nothing, and a touch user
    // nothing at all.
    const { fixture } = await mount('deck.pptx', PPTX_MIME);

    const label = fixture.nativeElement.textContent;
    expect(label).toContain('PREVIEW');

    const badge = [...fixture.nativeElement.querySelectorAll('span')].find(
      (el: HTMLElement) => el.textContent?.includes('PREVIEW'),
    ) as HTMLElement;
    expect(badge.className).not.toContain('opacity-0');
  });

  it('does not advertise Preview for a format the pane cannot render', async () => {
    const { fixture } = await mount('paper.pdf', PDF_MIME);
    expect(fixture.nativeElement.textContent).not.toContain('PREVIEW');
  });

  it('names the action Preview only when it previews', async () => {
    const deck = await mount('deck.pptx', PPTX_MIME);
    expect(
      deck.fixture.nativeElement.querySelector('button').getAttribute('aria-label'),
    ).toBe('Preview deck.pptx');

    const pdf = await mount('paper.pdf', PDF_MIME);
    expect(
      pdf.fixture.nativeElement.querySelector('button').getAttribute('aria-label'),
    ).toBe('Open paper.pdf');
  });
});
