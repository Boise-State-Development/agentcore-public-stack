import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DocxViewerComponent } from './docx-viewer.component';

const renderAsync = vi.fn();

// Intercepts the component's dynamic `import('docx-preview')` — the real
// library parses OOXML with jszip, which is neither fast nor the thing
// under test here.
vi.mock('docx-preview', () => ({
  renderAsync: (...args: unknown[]) => renderAsync(...args),
}));

describe('DocxViewerComponent', () => {
  let fixture: ComponentFixture<DocxViewerComponent>;

  beforeEach(async () => {
    renderAsync.mockReset();
    // The component memoizes its dynamic import('docx-preview') on a static
    // field, and the builder runs vitest with isolate: false, so a sibling spec
    // that rendered first would pin *its* mocked module for this file too.
    (DocxViewerComponent as unknown as { libraryPromise: unknown }).libraryPromise = null;
    renderAsync.mockImplementation((_data, host: HTMLElement) => {
      const wrapper = document.createElement('div');
      wrapper.className = 'docx-wrapper';
      const page = document.createElement('section');
      page.className = 'docx';
      // docx-preview stamps the page size inline, in points (US Letter).
      page.style.width = '612pt';
      page.style.minHeight = '792pt';
      page.textContent = 'rendered page';
      wrapper.appendChild(page);
      host.appendChild(wrapper);
      return Promise.resolve();
    });

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [DocxViewerComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(DocxViewerComponent);
  });

  /** Set the bytes input and let the render promise settle. */
  async function setBytes(bytes: ArrayBuffer | null): Promise<void> {
    fixture.componentRef.setInput('bytes', bytes);
    fixture.detectChanges();
    for (let i = 0; i < 20; i++) await Promise.resolve();
    fixture.detectChanges();
  }

  it('renders nothing and calls no library until bytes arrive', async () => {
    await setBytes(null);

    expect(renderAsync).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('.docx-host')?.childNodes.length).toBe(0);
  });

  it('renders the document and emits rendered', async () => {
    const rendered = vi.fn();
    fixture.componentInstance.rendered.subscribe(rendered);

    await setBytes(new ArrayBuffer(16));

    expect(renderAsync).toHaveBeenCalledOnce();
    expect(fixture.nativeElement.textContent).toContain('rendered page');
    expect(rendered).toHaveBeenCalledOnce();
  });

  it('passes the separate style container so injected CSS stays in our subtree', async () => {
    await setBytes(new ArrayBuffer(16));

    const [, bodyContainer, styleContainer] = renderAsync.mock.calls[0];
    expect(bodyContainer).toBeInstanceOf(HTMLElement);
    expect(styleContainer).toBeInstanceOf(HTMLElement);
    expect(bodyContainer).not.toBe(styleContainer);
  });

  it('clears the previous document before rendering a new one', async () => {
    await setBytes(new ArrayBuffer(16));
    await setBytes(new ArrayBuffer(32));

    // One page, not two stacked — the host is emptied on each change.
    expect(
      fixture.nativeElement.querySelectorAll('.docx-host section').length,
    ).toBe(1);
  });

  it('emits a readable message when the bytes are not a valid docx', async () => {
    renderAsync.mockRejectedValue(new Error('corrupt zip'));
    const failed = vi.fn();
    fixture.componentInstance.renderFailed.subscribe(failed);

    await setBytes(new ArrayBuffer(4));

    expect(failed).toHaveBeenCalledOnce();
    expect(failed.mock.calls[0][0]).toContain("couldn't be read as a Word document");
  });

  it('discards a render that resolves after the input changed again', async () => {
    // A large first document still parsing when the user opens a second
    // one must not paint over it.
    let resolveFirst: (() => void) | undefined;
    renderAsync.mockImplementationOnce((_d, host: HTMLElement) => {
      return new Promise<void>((resolve) => {
        resolveFirst = () => {
          const page = document.createElement('section');
          page.textContent = 'stale';
          host.appendChild(page);
          resolve();
        };
      });
    });

    fixture.componentRef.setInput('bytes', new ArrayBuffer(16));
    fixture.detectChanges();
    // Let the first render get past the dynamic import and actually
    // reach renderAsync, where it now hangs. Without this it would bail
    // on the sequence check and hand its `once` mock to the second
    // render instead — testing nothing.
    for (let i = 0; i < 20; i++) await Promise.resolve();
    expect(renderAsync).toHaveBeenCalledOnce();

    await setBytes(new ArrayBuffer(32));
    resolveFirst?.();
    for (let i = 0; i < 20; i++) await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('stale');
    expect(fixture.nativeElement.textContent).toContain('rendered page');
  });

  it('asks the renderer to keep the real page height', async () => {
    // ignoreHeight:true collapses a short document to the height of its
    // own text, so it stops reading as a page at all.
    await setBytes(new ArrayBuffer(16));

    const opts = renderAsync.mock.calls[0][3];
    expect(opts.ignoreHeight).toBe(false);
    expect(opts.breakPages).toBe(true);
    expect(opts.inWrapper).toBe(true);
  });

  it('scales a US-Letter page down to fit a narrower pane', async () => {
    const scroller = fixture.nativeElement.querySelector('.h-full');
    // 612pt = 816px natural width; report a 640px content box.
    vi.spyOn(scroller, 'clientWidth', 'get').mockReturnValue(640);

    await setBytes(new ArrayBuffer(16));

    // getComputedStyle reports no padding in jsdom, so the whole
    // clientWidth is the content box here: 640 / 816.
    const zoom = Number(fixture.nativeElement.style.getPropertyValue('--docx-zoom'));
    expect(zoom).toBeCloseTo(640 / 816, 3);
  });

  it('never enlarges a page past its true size', async () => {
    const scroller = fixture.nativeElement.querySelector('.h-full');
    vi.spyOn(scroller, 'clientWidth', 'get').mockReturnValue(1400);

    await setBytes(new ArrayBuffer(16));

    expect(
      Number(fixture.nativeElement.style.getPropertyValue('--docx-zoom')),
    ).toBe(1);
  });

  it('resets the zoom when the document is cleared', async () => {
    const scroller = fixture.nativeElement.querySelector('.h-full');
    vi.spyOn(scroller, 'clientWidth', 'get').mockReturnValue(640);

    await setBytes(new ArrayBuffer(16));
    await setBytes(null);

    expect(
      Number(fixture.nativeElement.style.getPropertyValue('--docx-zoom')),
    ).toBe(1);
  });

  describe('table conditional formatting', () => {
    /** Render a page containing one table with the given table-level flags. */
    async function renderTable(tableClass: string): Promise<HTMLTableElement> {
      renderAsync.mockImplementation((_data, host: HTMLElement) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'docx-wrapper';
        const page = document.createElement('section');
        page.className = 'docx';
        page.style.width = '612pt';
        page.innerHTML = `
          <table class="${tableClass}">
            <tr><td><p><span>Quarter</span></p></td><td><p><span>Rev</span></p></td></tr>
            <tr><td><p><span>Q1</span></p></td><td><p><span>100</span></p></td></tr>
            <tr><td><p><span>Q2</span></p></td><td><p><span>120</span></p></td></tr>
          </table>`;
        wrapper.appendChild(page);
        host.appendChild(wrapper);
        return Promise.resolve();
      });

      await setBytes(new ArrayBuffer(16));
      return fixture.nativeElement.querySelector('table') as HTMLTableElement;
    }

    it('tags the header row so the style sheet can reach it', async () => {
      // docx-preview puts the flags on the <table> but never on the <tr>,
      // so its own `tr.first-row td span` rule matches nothing.
      const table = await renderTable('first-row no-vband docx_lightgrid-accent1');

      expect(table.rows[0].classList.contains('first-row')).toBe(true);
      expect(table.rows[1].classList.contains('first-row')).toBe(false);
      expect(table.rows[2].classList.contains('first-row')).toBe(false);
    });

    it('tags the first column in every row when the flag is set', async () => {
      const table = await renderTable('first-col docx_lightgrid-accent1');

      for (const row of Array.from(table.rows)) {
        expect(row.cells[0].classList.contains('first-col')).toBe(true);
        expect(row.cells[1].classList.contains('first-col')).toBe(false);
      }
    });

    it('tags the last row and last column when those flags are set', async () => {
      const table = await renderTable('last-row last-col docx_lightgrid-accent1');

      expect(table.rows[2].classList.contains('last-row')).toBe(true);
      expect(table.rows[0].classList.contains('last-row')).toBe(false);
      for (const row of Array.from(table.rows)) {
        expect(row.cells[1].classList.contains('last-col')).toBe(true);
      }
    });

    it('adds nothing when the table enables no conditional bands', async () => {
      const table = await renderTable('no-hband no-vband docx_tablegrid');

      for (const row of Array.from(table.rows)) {
        expect(row.className).toBe('');
        for (const cell of Array.from(row.cells)) {
          expect(cell.className).toBe('');
        }
      }
    });

    it('bands the body rows, counting from the first row under the header', async () => {
      // Ground truth from Word: header + 5 data rows draws the fill on
      // data rows 1, 3 and 5.
      const table = await renderTable('first-row no-vband docx_lightgrid-accent1');

      expect(table.rows[0].className).toContain('first-row');
      expect(table.rows[0].className).not.toContain('odd-row');
      expect(table.rows[1].className).toContain('odd-row');
      expect(table.rows[2].className).toContain('even-row');
    });

    it('excludes a total row from the banding count', async () => {
      const table = await renderTable('first-row last-row no-vband docx_x');

      expect(table.rows[0].className).toContain('first-row');
      expect(table.rows[2].className).toContain('last-row');
      expect(table.rows[2].className).not.toMatch(/odd-row|even-row/);
      // Only the single middle row is body, so it is the first band.
      expect(table.rows[1].className).toContain('odd-row');
    });

    it('does not band rows when the table turns horizontal banding off', async () => {
      const table = await renderTable('first-row no-hband no-vband docx_x');

      const banded = Array.from(table.rows).filter((r) =>
        /odd-row|even-row/.test(r.className),
      );
      expect(banded).toHaveLength(0);
    });

    it('bands columns only when vertical banding is on', async () => {
      const off = await renderTable('no-vband docx_x');
      expect(off.querySelectorAll('td.odd-col')).toHaveLength(0);

      const on = await renderTable('docx_x');
      // Two body columns: the first takes the odd band, the second the
      // table's default styling (there is no `even-col` rule).
      expect(on.rows[0].cells[0].classList.contains('odd-col')).toBe(true);
      expect(on.rows[0].cells[1].classList.contains('odd-col')).toBe(false);
    });
  });
});
