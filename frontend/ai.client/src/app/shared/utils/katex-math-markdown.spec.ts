import { describe, it, expect, beforeAll } from 'vitest';
import { marked } from 'marked';
import { installKatexMathExtensions } from './katex-math-markdown';

describe('installKatexMathExtensions', () => {
  beforeAll(() => {
    installKatexMathExtensions();
  });

  it('preserves the backslashes in \\(...\\) that marked would otherwise eat', () => {
    // Unpatched, marked reads `\(` as an escaped paren and emits
    // `<p>Let (x^2) hold.</p>` — no delimiter left for KaTeX to match.
    expect(marked.parse('Let \\(x^2\\) hold.')).toContain('\\(x^2\\)');
  });

  it('preserves the backslashes in \\[...\\]', () => {
    expect(marked.parse('\\[a^2 + b^2 = c^2\\]')).toContain('\\[a^2 + b^2 = c^2\\]');
  });

  it('escapes the body so the sanitizer and innerHTML cannot alter it', () => {
    // `&` matters: AMS alignment environments are built from it, and it must
    // survive as a literal `&` in the text node KaTeX reads.
    const html = marked.parse('\\(a &= b\\)') as string;

    expect(html).toContain('&amp;=');

    const el = document.createElement('div');
    el.innerHTML = html;
    expect(el.textContent).toContain('\\(a &= b\\)');
  });

  it('leaves currency alone', () => {
    expect(marked.parse('Q1 ($100K), Q2 ($115K)')).toContain('Q1 ($100K), Q2 ($115K)');
  });

  it('does not reach into a code span', () => {
    const html = marked.parse('Write `\\(x^2\\)` for inline math.') as string;

    expect(html).toContain('<code>\\(x^2\\)</code>');
  });

  it('does not reach into a fenced code block', () => {
    const html = marked.parse(['```python', 'print("\\\\(not math\\\\)")', '```'].join('\n')) as string;

    expect(html).toContain('\\\\(not math\\\\)');
  });

  it('leaves an unclosed delimiter as ordinary markdown', () => {
    // A half-streamed formula must not swallow the rest of the message.
    const html = marked.parse('Let \\(x^2 and then some more prose.') as string;

    expect(html).toContain('and then some more prose.');
  });
});

describe('display math contents survive markdown', () => {
  beforeAll(() => {
    installKatexMathExtensions();
  });

  it('keeps asterisks in $$...$$ instead of turning them into emphasis', () => {
    // Unpatched: `$$a<em>b</em>c$$`. The asterisks are deleted AND the text
    // node is split in three, so KaTeX's `splitAtDelimiters` — which works
    // within one text node — no longer pairs the `$$` and renders nothing.
    const html = marked.parse('$$a*b*c$$') as string;

    expect(html).not.toContain('<em>');
    expect(html).toContain('$$a*b*c$$');
  });

  it('keeps \\\\ row breaks in $$...$$ instead of collapsing them', () => {
    // Unpatched: `\\` becomes `\`, which breaks every matrix and every
    // multi-row alignment.
    const html = marked.parse('$$\\begin{pmatrix} a \\\\ b \\end{pmatrix}$$') as string;

    expect(html).toContain('a \\\\ b');
  });

  it('keeps \\\\ row breaks in a bare \\begin{align} block', () => {
    const html = marked.parse('\\begin{align} a &= b \\\\ c &= d \\end{align}') as string;

    expect(html).toContain('\\\\');
    expect(html).toContain('\\begin{align}');
    expect(html).toContain('\\end{align}');
  });

  it('requires \\end to name the same environment as \\begin', () => {
    // The backreference keeps the rule from claiming an arbitrary span
    // between two unrelated environment markers. A mismatch is simply not
    // math, so it falls through to ordinary markdown — which is observable
    // because the `\\` is then collapsed rather than protected.
    const matched = marked.parse('\\begin{align} a \\\\ b \\end{align}') as string;
    const mismatched = marked.parse('\\begin{align} a \\\\ b \\end{gather}') as string;

    expect(matched).toContain('a \\\\ b');
    expect(mismatched).toContain('a \\ b');
    expect(mismatched).not.toContain('a \\\\ b');
  });

  it('leaves currency untouched by the $$ rule', () => {
    expect(marked.parse('Costs $5 and $10.')).toContain('Costs $5 and $10.');
  });

  it('does not let one $$ block swallow the next', () => {
    const html = marked.parse('$$a$$ and then $$b$$') as string;

    expect(html).toContain('$$a$$ and then $$b$$');
  });
});

describe('guarded $...$ inline math', () => {
  beforeAll(() => {
    installKatexMathExtensions();
  });

  /** What marked emits, with `\(…\)` marking what became math. */
  const parse = (src: string) => marked.parse(src) as string;

  it('rewrites genuine inline math to a delimiter KaTeX actually has', () => {
    // Never back to `$…$`: bare `$` is deliberately absent from
    // KATEX_DELIMITERS, because KaTeX pairs it positionally in the DOM.
    expect(parse('$ax^2 + bx + c = 0$')).toContain('\\(ax^2 + bx + c = 0\\)');
  });

  it.each([
    ['two amounts in prose', 'Q1 ($100K), Q2 ($115K), and the total ($215K)'],
    ['a range', 'Costs between $5 and $10 per seat.'],
    ['a price list', 'a tutoring session costs $40 and a full package is $300.'],
    ['a table row', '| Fall | $4,500 | $9,000 |'],
    ['a space after the sign', 'Prices $ 5 and $ 10'],
    ['non-numeric currency codes', '$USD 100 and $EUR 50'],
  ])('leaves %s alone', (_label, src) => {
    expect(parse(src)).not.toContain('\\(');
  });

  it('separates currency from math in one sentence', () => {
    const html = parse('Revenue rose from $1M to $2M, i.e. $r = 2$.');

    expect(html).toContain('$1M');
    expect(html).toContain('$2M');
    expect(html).toContain('\\(r = 2\\)');
  });

  it('does not span a line break', () => {
    // Without the newline bound, an unmatched `$` would reach across
    // paragraphs and swallow them.
    expect(parse('Costs $5\nand later $9 too.')).not.toContain('\\(');
  });

  it('does not reach into code', () => {
    expect(parse('Use `$HOME` and `$PATH` here.')).not.toContain('\\(');
  });

  it('leaves $$ display math to the display rule', () => {
    const html = parse('$$E = mc^2$$');

    expect(html).toContain('$$E = mc^2$$');
    expect(html).not.toContain('\\(');
  });
});
