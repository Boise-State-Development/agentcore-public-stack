import type { KatexOptions } from 'ngx-markdown';

/**
 * Delimiters handed to KaTeX's `renderMathInElement` for assistant markdown.
 *
 * This list exists to REMOVE one entry. ngx-markdown's own
 * `DEFAULT_KATEX_OPTIONS` adds `{ left: '$', right: '$' }`, which KaTeX
 * upstream deliberately leaves out of its defaults — the commented-out line in
 * `katex/dist/contrib/auto-render.js` says why:
 *
 *   // LaTeX uses $…$, but it ruins the display of normal `$` in text
 *
 * It ruins it because `renderMathInElement` walks text nodes and pairs dollar
 * signs positionally. Any two currency amounts on one line become a delimiter
 * pair and everything between them is swallowed into math mode, so
 *
 *   Q1 ($100K), Q2 ($115K), total ($215K)
 *
 * rendered as `Q1 (100K),Q2(115K), total ($215K)` — the sentence, not just the
 * numbers. Currency in prose is far more common here than inline math, and the
 * failure is destructive rather than cosmetic, so `$…$` goes.
 *
 * Inline `$…$` that a model writes still renders, but it is resolved one layer
 * earlier: `katex-math-markdown.ts` decides, with surrounding context that
 * KaTeX does not have here, whether a given `$` opens math or precedes an
 * amount, and rewrites only the former to `\(…\)`. Keeping bare `$` out of
 * THIS list is what makes that safe — KaTeX can never re-pair the dollars it
 * sees in the DOM and undo that decision.
 *
 * The same file is also why `\(…\)` and `\[…\]` appear here at all: marked
 * reads `\(` as an escaped paren, so without that protection these are not an
 * alternative to `$…$`, they are nothing at all.
 *
 * Note that escaping the dollar sign is NOT an alternative fix. A `&#36;` the
 * model writes is decoded to a literal `$` by the browser when marked's output
 * is assigned to `innerHTML`, which happens BEFORE KaTeX walks the DOM — the
 * entity form breaks identically. Verified against the app's own KaTeX build.
 */
export const KATEX_DELIMITERS: KatexOptions['delimiters'] = [
  { left: '$$', right: '$$', display: true },
  { left: '\\(', right: '\\)', display: false },
  { left: '\\[', right: '\\]', display: true },
  { left: '\\begin{equation}', right: '\\end{equation}', display: true },
  { left: '\\begin{align}', right: '\\end{align}', display: true },
  { left: '\\begin{alignat}', right: '\\end{alignat}', display: true },
  { left: '\\begin{gather}', right: '\\end{gather}', display: true },
  { left: '\\begin{CD}', right: '\\end{CD}', display: true },
];

/**
 * KaTeX options for assistant markdown. `throwOnError` is off so a malformed
 * expression renders as red source text instead of aborting the whole pass —
 * during streaming, every partially-arrived formula is malformed.
 */
export const KATEX_OPTIONS: KatexOptions = {
  delimiters: KATEX_DELIMITERS,
  throwOnError: false,
};
