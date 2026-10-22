import { marked, type TokenizerAndRendererExtension } from 'marked';

/**
 * Keeps LaTeX's `\(…\)` and `\[…\]` math delimiters intact through markdown
 * parsing, so KaTeX can find them when it walks the rendered DOM.
 *
 * Without this they never survive. CommonMark lists `(`, `)`, `[` and `]` as
 * escapable punctuation, so marked reads `\(` as "a literal paren" and drops
 * the backslash:
 *
 *   marked.parse('Let \\(x^2\\) hold.')  ->  '<p>Let (x^2) hold.</p>'
 *
 * By the time `renderMathInElement` runs there is no delimiter left to match,
 * which is why `\(…\)` silently rendered as plain text in this app while
 * `$$…$$` worked — a dollar sign is not escapable, so marked passes it
 * through untouched.
 *
 * These tokenizers claim the span before marked's escape rule can, and emit
 * the delimiters back verbatim with an HTML-escaped body. The output is plain
 * text, so Angular's sanitizer has nothing to strip, and the browser decodes
 * the entities back to their characters in the text node — leaving exactly
 * what KaTeX expects, including the `&` that AMS alignment environments use.
 *
 * Fenced code and code spans are unaffected: block-level fences never reach an
 * inline tokenizer, and marked's codespan rule consumes a span's contents
 * whole without re-tokenizing them.
 */

/** Escape only what would change the text node's meaning in innerHTML. */
function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function mathExtension(
  name: string,
  open: string,
  close: string,
  rule: RegExp,
): TokenizerAndRendererExtension {
  return {
    name,
    level: 'inline',
    start: (src: string) => {
      const at = src.indexOf(open);
      return at === -1 ? undefined : at;
    },
    tokenizer(src: string) {
      const match = rule.exec(src);
      if (!match) return undefined;
      return { type: name, raw: match[0], text: match[1] };
    },
    renderer: (token) => `${open}${escapeHtml(String(token['text']))}${close}`,
  };
}

/** `\(x^2\)` — inline math. */
const inlineMath = mathExtension('katexInlineMath', '\\(', '\\)', /^\\\(([\s\S]+?)\\\)/);

/** `\[x^2\]` — display math. */
const displayMath = mathExtension('katexDisplayMath', '\\[', '\\]', /^\\\[([\s\S]+?)\\\]/);

/**
 * `$$…$$` — display math. A dollar sign survives markdown on its own, so this
 * delimiter always reached KaTeX. Its *contents* did not:
 *
 *   `$$a*b*c$$`                            -> `$$a<em>b</em>c$$`
 *   `$$\begin{pmatrix} a \\ b \end{…}$$`   -> `$$\begin{pmatrix} a \ b \end{…}$$`
 *
 * Emphasis deletes the asterisks AND splits the text node in three, and
 * `splitAtDelimiters` works within a single text node — so the `$$` no longer
 * pair and nothing renders at all. `\\`, markdown's escaped backslash,
 * collapses to one, which breaks every matrix and every multi-row alignment.
 * Claiming the span keeps the body out of marked's inline rules entirely.
 */
const dollarDisplayMath = mathExtension('katexDollarMath', '$$', '$$', /^\$\$([\s\S]+?)\$\$/);

/**
 * `\begin{align}…\end{align}` and friends, used bare rather than wrapped in a
 * `$$`. The delimiters themselves survive — a backslash before a letter is not
 * a markdown escape — but the body has the same `\\` problem as above.
 */
const environmentMath: TokenizerAndRendererExtension = {
  name: 'katexEnvironmentMath',
  level: 'inline',
  start: (src: string) => {
    const at = src.indexOf('\\begin{');
    return at === -1 ? undefined : at;
  },
  tokenizer(src: string) {
    const match = /^\\begin\{([A-Za-z]+\*?)\}([\s\S]+?)\\end\{\1\}/.exec(src);
    if (!match) return undefined;
    return { type: 'katexEnvironmentMath', raw: match[0], environment: match[1], text: match[2] };
  },
  renderer: (token) => {
    const environment = String(token['environment']);
    return `\\begin{${environment}}${escapeHtml(String(token['text']))}\\end{${environment}}`;
  },
};

/**
 * `$x^2$` — inline math, but only where it cannot be currency.
 *
 * Bare `$…$` is deliberately NOT in `KATEX_DELIMITERS`, because KaTeX pairs
 * delimiters positionally inside a text node and would swallow the prose
 * between two dollar amounts. But the models keep writing it: `$…$` is the
 * dominant LaTeX convention, and a system-prompt line asking for `\(…\)`
 * does not reliably override that (measured — Haiku 4.5 wrote `$e^{i\pi} +
 * 1 = 0$` on the very turn the new instruction was live). Dropping it
 * outright therefore means inline math usually renders as literal source.
 *
 * So this rule recognises inline math here, where there is enough context to
 * tell it from money, and rewrites it to `\(…\)` — a delimiter KaTeX does
 * have. Currency is never rewritten, and because bare `$` never enters the
 * delimiter list, KaTeX's positional pairing cannot resurrect the bug no
 * matter what this rule does.
 *
 * The test is Pandoc's, plus a digit check for currency:
 *   - the opening `$` is not followed by whitespace or a digit  (`$40`)
 *   - the closing `$` is not preceded by whitespace              (`$ 5 … $`)
 *   - the closing `$` is not followed by a digit
 *   - the span does not cross a line break
 *
 * "Revenue rose from $1M to $2M, i.e. $r = 2$." resolves exactly right:
 * both amounts are skipped and only `$r = 2$` becomes math.
 */
const DOLLAR_INLINE_MATH = /^\$(?![\s\d])([^\n$]+?)\$(?!\d)/;

const guardedInlineMath: TokenizerAndRendererExtension = {
  name: 'katexGuardedInlineMath',
  level: 'inline',
  start: (src: string) => {
    const at = src.indexOf('$');
    return at === -1 ? undefined : at;
  },
  tokenizer(src: string) {
    const match = DOLLAR_INLINE_MATH.exec(src);
    // A trailing space before the closing `$` means this is prose, not math.
    if (!match || /\s$/.test(match[1])) return undefined;
    return { type: 'katexGuardedInlineMath', raw: match[0], text: match[1] };
  },
  // Emitted as `\(…\)` rather than `$…$`: KaTeX renders the former and never
  // pairs the latter, which is what keeps currency safe.
  renderer: (token) => `\\(${escapeHtml(String(token['text']))}\\)`,
};

/**
 * Order matters. `$$` is tried before the guarded single-`$` rule so that
 * display math is never mistaken for two inline spans, and before the
 * environment rule so `$$\begin{align}…\end{align}$$` is claimed by the outer
 * delimiter — the one KaTeX will render from.
 */
export const KATEX_MARKED_EXTENSIONS = [
  dollarDisplayMath,
  inlineMath,
  displayMath,
  environmentMath,
  guardedInlineMath,
];

/**
 * Registers the extensions on the module-level `marked` instance, which is
 * the one ngx-markdown parses with. Called once at bootstrap.
 */
export function installKatexMathExtensions(): void {
  marked.use({ extensions: KATEX_MARKED_EXTENSIONS });
}
