import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DocxViewerComponent } from './docx-viewer.component';

/** Stands in for docx-preview's render step: `(doc, host, options)`,
 *  painting into `host` the nodes `renderDocument` would return. */
const renderAsync = vi.fn();
/** The parsed document `parseAsync` hands back (the model the review reads). */
const parsed = vi.fn();

// Intercepts the component's dynamic `import('docx-preview')` — the real
// library parses OOXML with jszip, which is neither fast nor the thing
// under test here.
vi.mock('docx-preview', () => ({
  parseAsync: async () => parsed(),
  renderDocument: async (doc: unknown, options: unknown) => {
    const host = document.createElement('div');
    await renderAsync(doc, host, options);
    return Array.from(host.childNodes);
  },
}));

describe('DocxViewerComponent', () => {
  let fixture: ComponentFixture<DocxViewerComponent>;

  beforeEach(async () => {
    renderAsync.mockReset();
    parsed.mockReset();
    parsed.mockReturnValue({});
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
    // parse -> comments.xml read -> render -> decorate is a long chain of
    // awaits; drain it fully before the final change detection.
    for (let i = 0; i < 60; i++) await Promise.resolve();
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

  it('puts the library stylesheet in the hidden style host, not the document', async () => {
    renderAsync.mockImplementation((_doc, host: HTMLElement) => {
      const style = document.createElement('style');
      style.textContent = '.docx { color: black; }';
      const page = document.createElement('section');
      page.className = 'docx';
      host.append(style, page);
      return Promise.resolve();
    });

    await setBytes(new ArrayBuffer(16));

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.docx-host style')).toBeNull();
    expect(el.querySelector('[hidden] style')).not.toBeNull();
    expect(el.querySelector('.docx-host section.docx')).not.toBeNull();
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

    const opts = renderAsync.mock.calls[0][2];
    expect(opts.ignoreHeight).toBe(false);
    expect(opts.breakPages).toBe(true);
    expect(opts.inWrapper).toBe(true);
  });

  it('scales a US-Letter page down to fit a narrower pane', async () => {
    const scroller = fixture.nativeElement.querySelector('.docx-host').parentElement;
    // 612pt = 816px natural width; report a 640px content box.
    vi.spyOn(scroller, 'clientWidth', 'get').mockReturnValue(640);

    await setBytes(new ArrayBuffer(16));

    // getComputedStyle reports no padding in jsdom, so the whole
    // clientWidth is the content box here: 640 / 816.
    const zoom = Number(fixture.nativeElement.style.getPropertyValue('--docx-zoom'));
    expect(zoom).toBeCloseTo(640 / 816, 3);
  });

  it('never enlarges a page past its true size', async () => {
    const scroller = fixture.nativeElement.querySelector('.docx-host').parentElement;
    vi.spyOn(scroller, 'clientWidth', 'get').mockReturnValue(1400);

    await setBytes(new ArrayBuffer(16));

    expect(
      Number(fixture.nativeElement.style.getPropertyValue('--docx-zoom')),
    ).toBe(1);
  });

  it('resets the zoom when the document is cleared', async () => {
    const scroller = fixture.nativeElement.querySelector('.docx-host').parentElement;
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

  describe('review', () => {
    type Node = { type?: string; children?: Node[]; text?: string; id?: string; author?: string; date?: string };

    /** docx-preview's output for the parts the review reads (see
     *  docx-review.spec.ts for the same stand-in, and why references
     *  render as nothing). */
    function paint(node: Node, into: globalThis.Node): void {
      const tag = { paragraph: 'p', inserted: 'ins', deleted: 'del', run: 'span' }[node.type ?? ''];
      if (tag) {
        const el = document.createElement(tag);
        if (node.type === 'inserted' || node.type === 'deleted') {
          (el as HTMLModElement).dateTime = node.date ?? '';
        }
        into.appendChild(el);
        node.children?.forEach((c) => paint(c, el));
      } else if (node.type === 'text' || node.type === 'deletedText') {
        into.appendChild(document.createTextNode(node.text ?? ''));
      } else if (node.type === 'commentRangeStart' || node.type === 'commentRangeEnd') {
        const edge = node.type === 'commentRangeStart' ? 'start' : 'end';
        into.appendChild(document.createComment(`${edge} of comment #${node.id}`));
      } else {
        node.children?.forEach((c) => paint(c, into));
      }
    }

    const run = (text: string): Node => ({ type: 'run', children: [{ type: 'text', text }] });
    const W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
    const W14 = 'http://schemas.microsoft.com/office/word/2010/wordml';

    beforeEach(() => {
      const body: Node = {
        children: [
          {
            type: 'paragraph',
            children: [
              { type: 'commentRangeStart', id: '901' },
              run('The term is twelve months'),
              { type: 'commentRangeEnd', id: '901' },
            ],
          },
          {
            type: 'paragraph',
            children: [
              run('Governed by '),
              { type: 'deleted', author: 'Assistant', date: '2026-11-12T17:20:00Z', children: [{ type: 'run', children: [{ type: 'deletedText', text: 'Idaho' }] }] },
              { type: 'inserted', author: 'Assistant', date: '2026-11-12T17:20:00Z', children: [run('Delaware')] },
            ],
          },
        ],
      };
      const comments = [
        { id: '901', author: 'Dana Reviewer', initials: 'DR', date: '2026-11-01T09:30:00Z', children: [{ type: 'paragraph', children: [run('Can we do 24?')] }] },
        { id: '902', author: 'Sam Counsel', initials: 'SC', date: '2026-11-01T11:12:00Z', children: [{ type: 'paragraph', children: [run('Pending finance.')] }] },
      ];
      const xml =
        `<w:comments xmlns:w="${W}" xmlns:w14="${W14}">` +
        `<w:comment w:id="901"><w:p w14:paraId="AA"/></w:comment>` +
        `<w:comment w:id="902"><w:p w14:paraId="BB"/></w:comment></w:comments>`;

      parsed.mockReturnValue({
        documentPart: { body },
        commentsPart: { comments, path: 'word/comments.xml', _package: { load: async () => xml } },
        commentsExtendedPart: { commentMap: { AA: { done: false }, BB: { paraIdParent: 'AA' } } },
      });
      renderAsync.mockImplementation((doc: { documentPart: { body: Node } }, host: HTMLElement) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'docx-wrapper';
        const page = document.createElement('section');
        page.className = 'docx';
        page.style.width = '612pt';
        doc.documentPart.body.children?.forEach((c) => paint(c, page));
        wrapper.appendChild(page);
        host.appendChild(wrapper);
        return Promise.resolve();
      });
    });

    const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
    const cards = (): HTMLElement[] => Array.from(el().querySelectorAll<HTMLElement>('[data-review-key]'));

    it('summarises what there is to review', async () => {
      await setBytes(new ArrayBuffer(16));

      expect(el().textContent).toContain('1 comment · 1 tracked change · 3 reviewers');
    });

    it('lists the comment thread with its reply, then the change, in page order', async () => {
      await setBytes(new ArrayBuffer(16));

      const [thread, change] = cards();
      expect(cards()).toHaveLength(2);
      expect(thread.textContent).toContain('Dana Reviewer');
      expect(thread.textContent).toContain('Can we do 24?');
      expect(thread.textContent).toContain('Sam Counsel');
      expect(thread.textContent).toContain('Pending finance.');
      expect(change.textContent).toContain('Assistant');
      expect(change.textContent).toContain('Replaced');
      expect(change.querySelector('del')?.textContent).toBe('Idaho');
      expect(change.querySelector('ins')?.textContent).toBe('Delaware');
    });

    it('marks the commented passage with a numbered marker naming the thread', async () => {
      await setBytes(new ArrayBuffer(16));

      const markers = el().querySelectorAll('.docx-host .docx-review-marker');
      expect(markers).toHaveLength(1);
      expect(markers[0].textContent).toBe('1');
      expect(markers[0].getAttribute('aria-label')).toBe('Comment 1 by Dana Reviewer, 1 reply');
    });

    it('selects the thread in the list when its marker is clicked', async () => {
      await setBytes(new ArrayBuffer(16));

      el().querySelector<HTMLElement>('.docx-review-marker')!.click();
      fixture.detectChanges();

      expect(cards()[0].getAttribute('aria-current')).toBe('true');
      expect(el().querySelector('.docx-review-marker')?.getAttribute('aria-current')).toBe('true');
    });

    it('marks both halves of a replacement in the page when its card is clicked', async () => {
      await setBytes(new ArrayBuffer(16));

      cards()[1].click();
      fixture.detectChanges();

      const selected = Array.from(el().querySelectorAll('.docx-host .docx-review-selected'));
      expect(selected.map((e) => e.tagName)).toEqual(['DEL', 'INS']);
      expect(cards()[1].getAttribute('aria-current')).toBe('true');
    });

    it('filters the list to comments or changes', async () => {
      await setBytes(new ArrayBuffer(16));

      const filter = (label: string) =>
        Array.from(el().querySelectorAll<HTMLButtonElement>('[role="group"] button')).find((b) =>
          b.textContent?.includes(label),
        )!;
      filter('Changes').click();
      fixture.detectChanges();

      expect(cards().map((c) => c.dataset['reviewKey'])).toEqual(['r:1']);
      expect(filter('Changes').getAttribute('aria-pressed')).toBe('true');
    });

    it('hides and shows the list from the toolbar', async () => {
      await setBytes(new ArrayBuffer(16));
      const toggle = el().querySelector<HTMLButtonElement>('[aria-controls="docx-review-panel"]')!;
      expect(toggle.getAttribute('aria-expanded')).toBe('true');

      toggle.click();
      fixture.detectChanges();

      expect(el().querySelector('#docx-review-panel')).toBeNull();
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(toggle.textContent).toContain('Show review');
    });

    it('shows no review chrome for a document with nothing to review', async () => {
      parsed.mockReturnValue({ documentPart: { body: { children: [{ type: 'paragraph', children: [run('plain')] }] } } });

      await setBytes(new ArrayBuffer(16));

      expect(el().querySelector('[aria-controls="docx-review-panel"]')).toBeNull();
      expect(el().querySelector('#docx-review-panel')).toBeNull();
    });

    it('drops the review when the document is cleared', async () => {
      await setBytes(new ArrayBuffer(16));
      await setBytes(null);

      expect(cards()).toHaveLength(0);
    });
  });
});
