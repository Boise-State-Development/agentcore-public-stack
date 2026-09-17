import { describe, it, expect } from 'vitest';
import { KATEX_DELIMITERS, KATEX_OPTIONS } from './katex-delimiters';

/**
 * Configuration guards. The rendering behaviour these produce is covered
 * end-to-end, against the real KaTeX, in
 * `session/components/message-list/components/streaming-text.katex.spec.ts`.
 */
describe('KATEX_DELIMITERS', () => {
  it('does not pair bare dollar signs', () => {
    // The whole point of the file: ngx-markdown's own DEFAULT_KATEX_OPTIONS
    // add `{ left: '$', right: '$' }`, which KaTeX upstream deliberately
    // omits because it mangles currency in prose.
    expect(KATEX_DELIMITERS).not.toContainEqual(
      expect.objectContaining({ left: '$', right: '$' }),
    );
  });

  it('keeps the delimiters that do work', () => {
    const pairs = (KATEX_DELIMITERS ?? []).map(({ left, right }) => `${left}${right}`);

    expect(pairs).toContain('$$$$');
    expect(pairs).toContain('\\(\\)');
    expect(pairs).toContain('\\[\\]');
    expect(pairs).toContain('\\begin{align}\\end{align}');
  });

  it('keeps throwOnError off so a half-streamed formula cannot abort the pass', () => {
    // Every partially-arrived formula is malformed while the typewriter runs.
    expect(KATEX_OPTIONS.throwOnError).toBe(false);
  });

  it('passes the delimiter list through as its options', () => {
    expect(KATEX_OPTIONS.delimiters).toBe(KATEX_DELIMITERS);
  });
});
