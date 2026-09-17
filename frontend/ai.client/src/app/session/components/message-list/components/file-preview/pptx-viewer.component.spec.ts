import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { PptxViewerComponent } from './pptx-viewer.component';

/** Slides the fake previewer will paint on the next `preview()`. */
let slidesToRender = 1;
/** Rejection for the next `preview()`, if the test wants one. */
let previewRejection: Error | null = null;

const init = vi.fn();

// Intercepts the component's dynamic `import('pptx-preview')`. The real
// library unzips the OOXML and lays out every shape, which is neither
// fast nor the thing under test: what matters here is that the component
// swaps the deck in only on success, discards a stale render, and treats
// a zero-slide result as a failure.
vi.mock('pptx-preview', () => ({
  init: (dom: HTMLElement, options: unknown) => {
    init(dom, options);
    return {
      get slideCount() {
        return slidesToRender;
      },
      preview: async () => {
        if (previewRejection) throw previewRejection;
        for (let i = 0; i < slidesToRender; i++) {
          const slide = document.createElement('div');
          slide.className = 'pptx-preview-slide-wrapper';
          dom.appendChild(slide);
        }
      },
      destroy: () => undefined,
    };
  },
}));

describe('PptxViewerComponent', () => {
  let fixture: ComponentFixture<PptxViewerComponent>;

  beforeEach(async () => {
    init.mockReset();
    slidesToRender = 1;
    previewRejection = null;

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [PptxViewerComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(PptxViewerComponent);
  });

  /**
   * Set bytes and let the async render settle.
   *
   * The component caches its dynamic `import('pptx-preview')` in a
   * static field, so whichever test runs first pays for resolving the
   * module graph — more turns of the event loop than a single
   * `whenStable()` covers. Draining a fixed number of macrotasks keeps
   * that cost off the assertions without depending on which test the
   * runner happens to schedule first.
   */
  async function render(bytes: ArrayBuffer | null): Promise<void> {
    fixture.componentRef.setInput('bytes', bytes);
    fixture.detectChanges();

    for (let i = 0; i < 10; i++) {
      await new Promise((resolve) => setTimeout(resolve, 0));
      await fixture.whenStable();
      fixture.detectChanges();
    }
  }

  function slides(): NodeListOf<Element> {
    return fixture.nativeElement.querySelectorAll('.pptx-preview-slide-wrapper');
  }

  it('paints the deck and reports it rendered', async () => {
    slidesToRender = 3;
    const rendered = vi.fn();
    fixture.componentInstance.rendered.subscribe(rendered);

    await render(new ArrayBuffer(16));

    expect(slides().length).toBe(3);
    expect(rendered).toHaveBeenCalledTimes(1);
  });

  it('renders at a fixed width and scales to fit rather than re-parsing', async () => {
    await render(new ArrayBuffer(16));

    // The library has no responsive mode — it lays the deck out against
    // the size given at init. Re-initialising on a rail drag would
    // re-parse the file, so the width must not come from the pane.
    expect(init).toHaveBeenCalledTimes(1);
    expect(init.mock.calls[0][1]).toMatchObject({ mode: 'list' });
    const { width, height } = init.mock.calls[0][1] as {
      width: number;
      height: number;
    };
    expect(width).toBeGreaterThan(0);
    // 16:9, the ratio the PowerPoint tool always emits.
    expect(Math.round((width * 9) / 16)).toBe(height);
  });

  it('treats a deck that parsed into zero slides as a failure', async () => {
    // `pptx-preview` resolves successfully when it cannot make sense of
    // a presentation's theme or layout parts, returning no slides.
    // Without this guard the pane paints empty, with no error and no
    // retry, which reads as the app being broken rather than the file
    // being unreadable.
    slidesToRender = 0;
    const failed = vi.fn();
    const rendered = vi.fn();
    fixture.componentInstance.renderFailed.subscribe(failed);
    fixture.componentInstance.rendered.subscribe(rendered);

    await render(new ArrayBuffer(16));

    expect(failed).toHaveBeenCalledTimes(1);
    expect(failed.mock.calls[0][0]).toContain('PowerPoint');
    expect(rendered).not.toHaveBeenCalled();
    expect(slides().length).toBe(0);
  });

  it('reports a parse failure without painting a partial deck', async () => {
    previewRejection = new Error('corrupt zip');
    const failed = vi.fn();
    fixture.componentInstance.renderFailed.subscribe(failed);

    await render(new ArrayBuffer(16));

    expect(failed).toHaveBeenCalledTimes(1);
    expect(slides().length).toBe(0);
  });

  it('clears the deck when the bytes go away', async () => {
    await render(new ArrayBuffer(16));
    expect(slides().length).toBe(1);

    await render(null);

    expect(slides().length).toBe(0);
  });
});
