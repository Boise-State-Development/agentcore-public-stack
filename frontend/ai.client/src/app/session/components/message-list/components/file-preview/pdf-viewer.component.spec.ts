import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { PdfViewerComponent } from './pdf-viewer.component';
import { PdfJsLoader } from './pdfjs-loader';

/**
 * A stand-in for pdf.js. jsdom has no canvas and no module workers, so the
 * real library cannot run here; what is under test is the component's own
 * work: one slot per page, the first page drawn before `rendered`, a copy
 * of the bytes handed over, stale opens discarded, and failures named.
 */
interface FakeDocument {
  numPages: number;
  /** Pages whose draw rejects, 1-based. */
  brokenPages?: number[];
}

let nextDocument: FakeDocument = { numPages: 3 };
let openRejection: Error | null = null;
/** When set, `getDocument` waits on this before resolving. */
let openGate: Promise<void> | null = null;

const opened: Uint8Array[] = [];
const destroyed = vi.fn();
const drawn: number[] = [];

function namedError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

const fakePdfJs = {
  getDocument: (params: { data: Uint8Array }) => {
    opened.push(params.data);
    const doc = { ...nextDocument };
    const rejection = openRejection;
    const gate = openGate;
    return {
      promise: (async () => {
        if (gate) await gate;
        if (rejection) throw rejection;
        return {
          numPages: doc.numPages,
          getPage: async (pageNumber: number) => ({
            getViewport: ({ scale }: { scale: number }) => ({
              width: 600 * scale,
              height: 800 * scale,
            }),
            render: () => ({
              promise: (async () => {
                if (doc.brokenPages?.includes(pageNumber)) {
                  throw new Error('bad page');
                }
                drawn.push(pageNumber);
              })(),
              cancel: () => undefined,
            }),
            streamTextContent: () => `text of page ${pageNumber}`,
          }),
        };
      })(),
      destroy: async () => destroyed(),
    };
  },
  TextLayer: class {
    private readonly container: HTMLElement;
    private readonly text: string;
    constructor(options: { container: HTMLElement; textContentSource: string }) {
      this.container = options.container;
      this.text = options.textContentSource;
    }
    async render(): Promise<void> {
      const span = document.createElement('span');
      span.textContent = this.text;
      this.container.append(span);
    }
    cancel(): void {
      /* nothing in flight to stop */
    }
  },
};

const fakeLoader = {
  load: async () => fakePdfJs,
  assetUrl: (folder: string) => `/pdfjs/${folder}/`,
};

describe('PdfViewerComponent', () => {
  let fixture: ComponentFixture<PdfViewerComponent>;
  let rendered: number;
  let failures: string[];

  beforeEach(async () => {
    nextDocument = { numPages: 3 };
    openRejection = null;
    openGate = null;
    opened.length = 0;
    drawn.length = 0;
    destroyed.mockReset();

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [PdfViewerComponent],
      providers: [{ provide: PdfJsLoader, useValue: fakeLoader }],
    }).compileComponents();

    fixture = TestBed.createComponent(PdfViewerComponent);
    rendered = 0;
    failures = [];
    fixture.componentInstance.rendered.subscribe(() => rendered++);
    fixture.componentInstance.renderFailed.subscribe((m) => failures.push(m));
    fixture.detectChanges();

    // jsdom does no layout, so give the scroller a width to fit pages to.
    const scroller = fixture.nativeElement.querySelector('div') as HTMLElement;
    Object.defineProperty(scroller, 'clientWidth', { value: 480, configurable: true });
  });

  async function settle(): Promise<void> {
    for (let i = 0; i < 10; i++) {
      await new Promise((resolve) => setTimeout(resolve, 0));
      fixture.detectChanges();
    }
  }

  async function render(bytes: ArrayBuffer | null): Promise<void> {
    fixture.componentRef.setInput('bytes', bytes);
    fixture.detectChanges();
    await settle();
  }

  function pages(): HTMLElement[] {
    return Array.from(fixture.nativeElement.querySelectorAll('.pdf-page'));
  }

  it('lays out one labelled slot per page and reports the first paint', async () => {
    await render(new ArrayBuffer(8));

    expect(pages().map((p) => p.getAttribute('aria-label'))).toEqual([
      'Page 1 of 3',
      'Page 2 of 3',
      'Page 3 of 3',
    ]);
    expect(
      fixture.nativeElement.querySelector('[role="document"]').getAttribute('aria-label'),
    ).toBe('PDF, 3 pages');
    expect(pages()[0].querySelector('canvas')).not.toBeNull();
    expect(rendered).toBe(1);
    expect(failures).toEqual([]);
  });

  it('puts a text layer over each drawn page, so its text can be selected and read', async () => {
    await render(new ArrayBuffer(8));

    const text = pages()[0].querySelector('.textLayer');
    expect(text?.textContent).toBe('text of page 1');
    expect(pages()[0].querySelector('canvas')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('fits pages to the pane and records the scale for the text layer', async () => {
    await render(new ArrayBuffer(8));

    const first = pages()[0];
    // 480px of pane over a 600pt-wide page.
    expect(first.style.getPropertyValue('--total-scale-factor')).toBe('0.8');
    expect(first.style.aspectRatio).toBe('600 / 800');
  });

  it('hands pdf.js a copy, so the bytes the panel holds survive a retry', async () => {
    const bytes = new Uint8Array([37, 80, 68, 70]).buffer;
    await render(bytes);

    expect(opened[0].buffer).not.toBe(bytes);
    expect(Array.from(opened[0])).toEqual([37, 80, 68, 70]);
    expect(bytes.byteLength).toBe(4);
  });

  it('keeps the document when a later page cannot be drawn', async () => {
    nextDocument = { numPages: 3, brokenPages: [2] };
    await render(new ArrayBuffer(8));

    expect(rendered).toBe(1);
    expect(failures).toEqual([]);
    expect(drawn).toContain(3);
    expect(pages()[1].querySelector('canvas')).toBeNull();
  });

  it('fails when the first page cannot be drawn', async () => {
    nextDocument = { numPages: 2, brokenPages: [1] };
    await render(new ArrayBuffer(8));

    expect(rendered).toBe(0);
    expect(failures).toHaveLength(1);
    expect(pages()).toHaveLength(0);
  });

  it('says so when the PDF is password-protected', async () => {
    openRejection = namedError('PasswordException');
    await render(new ArrayBuffer(8));

    expect(failures).toEqual([
      'This PDF is password-protected, so it can’t be previewed here. Download it to open it.',
    ]);
    expect(rendered).toBe(0);
  });

  it('reports bytes that are not a PDF', async () => {
    openRejection = namedError('InvalidPDFException');
    await render(new ArrayBuffer(8));

    expect(failures).toEqual([
      'This file couldn’t be read as a PDF. It may be damaged or incomplete.',
    ]);
  });

  it('treats a document with no pages as unreadable', async () => {
    nextDocument = { numPages: 0 };
    await render(new ArrayBuffer(8));

    expect(failures).toHaveLength(1);
    expect(rendered).toBe(0);
  });

  it('discards an open that finishes after the bytes changed', async () => {
    let release!: () => void;
    openGate = new Promise<void>((resolve) => (release = resolve));
    nextDocument = { numPages: 5 };
    fixture.componentRef.setInput('bytes', new ArrayBuffer(8));
    fixture.detectChanges();
    await settle();

    openGate = null;
    nextDocument = { numPages: 2 };
    await render(new ArrayBuffer(16));
    release();
    await settle();

    expect(pages()).toHaveLength(2);
    expect(rendered).toBe(1);
  });

  it('clears the pages and frees the document when the bytes go away', async () => {
    await render(new ArrayBuffer(8));
    expect(pages()).toHaveLength(3);

    await render(null);

    expect(pages()).toHaveLength(0);
    expect(destroyed).toHaveBeenCalled();
  });
});
