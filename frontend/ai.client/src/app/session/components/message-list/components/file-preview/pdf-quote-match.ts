/**
 * Finding a cited quote in a page's text, so the PDF viewer can highlight it.
 *
 * The quote was located on the server against the backend's own extraction
 * of the PDF (`apis/shared/files/source_provenance.py`); here it has to be
 * found again in pdf.js's text layer, which is a different extraction of
 * the same page. Two things make that work:
 *
 * - **The same character folds as the server** (`_CHAR_FOLDS`, `_DROPPED`):
 *   smart quotes, dash variants, odd spaces and ligatures fold, and soft
 *   hyphens and zero-width characters drop. A quote the server matched
 *   only after folding must fold the same way here, or it would be
 *   verified and then not highlighted.
 * - **Whitespace is ignored entirely**, not just collapsed. pdf.js splits a
 *   page into positioned runs and the two extractors disagree about where
 *   spaces and line breaks go, so "the quick" may be two runs with no space
 *   between them. Letters alone are what both extractions agree on.
 *
 * Pure functions over strings, so the matching is testable without a PDF.
 */

/** Mirrors `_CHAR_FOLDS` in `source_provenance.py`. */
const CHAR_FOLDS: Readonly<Record<string, string>> = {
  '‘': "'",
  '’': "'",
  '‚': "'",
  '‛': "'",
  '“': '"',
  '”': '"',
  '„': '"',
  '‐': '-',
  '‑': '-',
  '‒': '-',
  '–': '-',
  '—': '-',
  '―': '-',
  '−': '-',
  ' ': ' ',
  ' ': ' ',
  ' ': ' ',
  ' ': ' ',
  'ﬁ': 'fi',
  'ﬂ': 'fl',
};

/** Mirrors `_DROPPED` in `source_provenance.py`. */
const DROPPED = new Set(['­', '﻿', '​', '‌', '‍']);

/**
 * A quote's start must be at least this long to be highlighted as the part
 * of a page-straddling passage that lies on this page. Shorter, and a few
 * common letters at the foot of a page would pass for the quote.
 */
const MIN_STRADDLE_CHARS = 24;

/** Where a normalized character came from: which text, which offset. */
interface Origin {
  text: number;
  offset: number;
}

function normalize(texts: readonly string[]): { value: string; origins: Origin[] } {
  let value = '';
  const origins: Origin[] = [];
  texts.forEach((text, index) => {
    for (let offset = 0; offset < text.length; offset++) {
      const ch = text[offset];
      if (DROPPED.has(ch)) continue;
      for (const c of CHAR_FOLDS[ch] ?? ch) {
        if (/\s/.test(c)) continue;
        value += c.toLowerCase();
        origins.push({ text: index, offset });
      }
    }
  });
  return { value, origins };
}

/** A run of characters to highlight inside one of the searched texts. */
export interface QuoteRange {
  /** Index into the texts that were searched. */
  text: number;
  start: number;
  /** Exclusive. */
  end: number;
}

/**
 * Where `quote` occurs across `texts` (a page's text runs, in reading
 * order), as one range per run it touches, or an empty list if it is not
 * there.
 *
 * A quote the server located on this page but that runs onto the next one
 * is not on this page whole. Then the part that is — the longest start of
 * the quote that ends the page — is matched instead, if it is long enough
 * to be the quote rather than a coincidence.
 */
export function findQuote(texts: readonly string[], quote: string): QuoteRange[] {
  const page = normalize(texts);
  const wanted = normalize([quote]).value;
  if (!wanted || !page.value) return [];

  let start = page.value.indexOf(wanted);
  let length = wanted.length;
  if (start === -1) {
    length = straddlingLength(page.value, wanted);
    if (length === 0) return [];
    start = page.value.length - length;
  }
  return toRanges(page.origins.slice(start, start + length));
}

/** Length of the longest start of `quote` that ends `page`, or 0. */
function straddlingLength(page: string, quote: string): number {
  for (let length = Math.min(quote.length - 1, page.length); length >= MIN_STRADDLE_CHARS; length--) {
    if (page.endsWith(quote.slice(0, length))) return length;
  }
  return 0;
}

function toRanges(origins: readonly Origin[]): QuoteRange[] {
  const ranges: QuoteRange[] = [];
  for (const { text, offset } of origins) {
    const last = ranges[ranges.length - 1];
    if (last && last.text === text) {
      last.end = offset + 1;
    } else {
      ranges.push({ text, start: offset, end: offset + 1 });
    }
  }
  return ranges;
}
