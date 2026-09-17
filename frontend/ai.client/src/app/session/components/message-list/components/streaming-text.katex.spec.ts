import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, beforeAll, afterAll } from 'vitest';
import { provideMarkdown } from 'ngx-markdown';
import katex from 'katex';
import renderMathInElement from 'katex/contrib/auto-render';
import ClipboardJS from 'clipboard';
import { StreamingTextComponent } from './streaming-text.component';
import { installLazyMermaid } from '../../../../shared/utils/lazy-mermaid';
import { installKatexMathExtensions } from '../../../../shared/utils/katex-math-markdown';

/**
 * End-to-end math rendering through the real pipeline: marked parses the
 * markdown, Angular's sanitizer cleans it, then KaTeX's `renderMathInElement`
 * walks the resulting DOM. Nothing is stubbed — `katex.min.js` and
 * `auto-render.min.js` are in angular.json's `scripts`, so they are on the
 * global scope here exactly as they are in the browser.
 *
 * Regression under test: ngx-markdown's DEFAULT_KATEX_OPTIONS pair bare
 * `$…$`, and `renderMathInElement` pairs delimiters positionally within a
 * text node. Two currency amounts on one line therefore became an inline
 * formula that swallowed the prose between them.
 */
describe('StreamingTextComponent KaTeX rendering', () => {
  let fixture: ComponentFixture<StreamingTextComponent>;

  // In the browser these arrive as globals from angular.json's `scripts`,
  // which the test build emits but does not evaluate. Publish them the same
  // way here — ngx-markdown reads them off the global scope — and take them
  // back down afterwards so no other spec file inherits them.
  const globals = globalThis as Record<string, unknown>;
  beforeAll(() => {
    globals['katex'] = katex;
    globals['renderMathInElement'] = renderMathInElement;
    // `clipboard` and `mermaid` are the template's other ngx-markdown
    // plugins; both throw on a missing global even for markdown that uses
    // neither. angular.json's `scripts` supplies ClipboardJS in the browser.
    globals['ClipboardJS'] = ClipboardJS;
    // The template also carries `mermaid`, whose plugin throws on a missing
    // global even for markdown with no diagram in it. app.config installs the
    // same stand-in at bootstrap.
    installLazyMermaid();
    // app.config registers these at bootstrap; they keep `\(…\)` from being
    // eaten by marked's escape rule before KaTeX sees it.
    installKatexMathExtensions();
  });
  afterAll(() => {
    delete globals['katex'];
    delete globals['renderMathInElement'];
    delete globals['ClipboardJS'];
    delete globals['mermaid'];
  });

  /** Render `markdown` as a finished (non-streaming) assistant message. */
  async function render(markdown: string): Promise<HTMLElement> {
    fixture.componentRef.setInput('text', markdown);
    fixture.componentRef.setInput('isStreaming', false);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [StreamingTextComponent],
      providers: [provideMarkdown()],
    }).compileComponents();

    fixture = TestBed.createComponent(StreamingTextComponent);
  });

  it('has KaTeX available, so the assertions below are meaningful', () => {
    // Without this guard a missing global would make every "no math rendered"
    // expectation below pass for the wrong reason.
    expect(typeof (globalThis as Record<string, unknown>)['katex']).not.toBe('undefined');
    expect(typeof (globalThis as Record<string, unknown>)['renderMathInElement']).not.toBe(
      'undefined',
    );
  });

  it('leaves a sentence of currency amounts intact', async () => {
    // The reported conversation, verbatim. Previously rendered as
    // "Q1 (100K),Q2(115K), total ($215K)" — the prose vanished into a formula.
    const el = await render('A table slide showing Q1 ($100K), Q2 ($115K), and the total ($215K)');

    expect(el.querySelectorAll('.katex')).toHaveLength(0);
    expect(el.textContent).toContain('Q1 ($100K), Q2 ($115K), and the total ($215K)');
  });

  it('leaves a markdown table of dollar amounts intact', async () => {
    const el = await render(
      ['| Term | Tuition |', '| --- | --- |', '| Fall | $4,500 |', '| Spring | $9,000 |'].join(
        '\n',
      ),
    );

    expect(el.querySelectorAll('.katex')).toHaveLength(0);
    expect(el.textContent).toContain('$4,500');
    expect(el.textContent).toContain('$9,000');
  });

  it('renders inline math written as \\(...\\)', async () => {
    const el = await render('Let \\(x^2 + y^2 = r^2\\) hold.');

    expect(el.querySelectorAll('.katex').length).toBeGreaterThan(0);
    // Rendered inline, inside the sentence — not lifted into its own block.
    expect(el.querySelectorAll('.katex-display')).toHaveLength(0);
    expect(el.textContent).toContain('Let ');
    expect(el.textContent).toContain(' hold.');
  });

  it('renders display math written as \\[...\\]', async () => {
    const el = await render('\\[a^2 + b^2 = c^2\\]');

    expect(el.querySelectorAll('.katex').length).toBeGreaterThan(0);
  });

  it('leaves math delimiters inside a code span alone', async () => {
    // The tokenizers must not reach into code. KaTeX already skips <code>,
    // so a rewrite here would corrupt the displayed source instead.
    const el = await render('Write `\\(x^2\\)` for inline math.');

    expect(el.querySelectorAll('.katex')).toHaveLength(0);
    expect(el.querySelector('code')?.textContent).toBe('\\(x^2\\)');
  });

  it('renders display math written as $$...$$', async () => {
    const el = await render('$$\\int_0^1 x^2 dx$$');

    expect(el.querySelectorAll('.katex').length).toBeGreaterThan(0);
  });

  it('renders a matrix, whose \\\\ row breaks markdown would otherwise collapse', async () => {
    const el = await render('$$\\begin{pmatrix} a \\\\ b \\end{pmatrix}$$');

    expect(el.querySelectorAll('.katex').length).toBeGreaterThan(0);
    // A collapsed `\\` leaves KaTeX an unknown control sequence, which it
    // renders in its error colour rather than as a matrix.
    expect(el.querySelector('.katex-error')).toBeNull();
  });

  it('renders display math containing asterisks', async () => {
    // Emphasis used to eat the asterisks and split the text node, leaving the
    // `$$` unpaired so nothing rendered at all.
    const el = await render('$$a*b*c$$');

    expect(el.querySelectorAll('.katex').length).toBeGreaterThan(0);
    expect(el.querySelector('em')).toBeNull();
  });

  it('renders a bare \\begin{align} block', async () => {
    const el = await render('\\begin{align} a &= b \\\\ c &= d \\end{align}');

    expect(el.querySelectorAll('.katex').length).toBeGreaterThan(0);
    expect(el.querySelector('.katex-error')).toBeNull();
  });

  it('renders inline math the model wrote as $...$', async () => {
    // Measured from a live turn: the models write `$...$` for inline math
    // whatever the system prompt asks for, so it has to render.
    const el = await render("Euler's identity states that $e^{i\\pi} + 1 = 0$, elegantly.");

    expect(el.querySelectorAll('.katex').length).toBe(1);
    expect(el.querySelectorAll('.katex-display')).toHaveLength(0);
    expect(el.textContent).toContain('elegantly.');
  });

  it('tells currency and inline math apart in the same sentence', async () => {
    const el = await render('Revenue rose from $1M to $2M, i.e. $r = 2$.');

    expect(el.querySelectorAll('.katex').length).toBe(1);
    expect(el.textContent).toContain('$1M');
    expect(el.textContent).toContain('$2M');
  });

  it('does not treat an HTML-entity dollar sign as escaped', async () => {
    // Documents why the old system-prompt rule was removed rather than kept as
    // a belt-and-braces measure: marked passes `&#36;` through to innerHTML,
    // the browser decodes it to a literal `$` in the text node, and KaTeX
    // walks the DOM afterwards. The entity is indistinguishable from a plain
    // `$` by the time math rendering happens — it only ever leaked into
    // generated files. With the delimiter gone, both forms are now safe.
    const el = await render('Q1 (&#36;100K), Q2 (&#36;115K)');

    expect(el.querySelectorAll('.katex')).toHaveLength(0);
    expect(el.textContent).toContain('$100K');
    expect(el.textContent).not.toContain('&#36;');
  });
});
