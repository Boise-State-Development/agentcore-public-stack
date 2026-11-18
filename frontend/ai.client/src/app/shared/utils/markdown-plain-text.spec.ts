import { describe, expect, it } from 'vitest';
import { markdownBlocks, markdownToPlainText } from './markdown-plain-text';

describe('markdownBlocks', () => {
  it('turns a crawled page excerpt into readable blocks', () => {
    const excerpt =
      '# Home - DRED at Boise State University\n\n## Supporting research from inception to completion\n\n' +
      'Boise State University is committed to fostering an environment where research thrives.\n\n' +
      '**The Division of Research and Economic Development leads this effort by:**\n\n' +
      '- providing comprehensive support for faculty\n- managing the university’s intellectual property portfolio';
    expect(markdownBlocks(excerpt)).toEqual([
      'Home - DRED at Boise State University',
      'Supporting research from inception to completion',
      'Boise State University is committed to fostering an environment where research thrives.',
      'The Division of Research and Economic Development leads this effort by:',
      'providing comprehensive support for faculty',
      'managing the university’s intellectual property portfolio',
    ]);
  });

  it('keeps link and image text, drops their URLs', () => {
    expect(markdownToPlainText('See [the research office](https://example.edu/r) and ![campus map](m.png).')).toBe(
      'See the research office and campus map.',
    );
  });

  it('drops emphasis, code markers and HTML tags but keeps their text', () => {
    expect(markdownToPlainText('A *b* __c__ ~~d~~ `e<1>` <span>f</span>')).toBe('A b c d e<1> f');
  });

  it('reads tables row by row and code blocks as text', () => {
    expect(markdownBlocks('| Name | Room |\n|---|---|\n| Lab | 101 |\n\n```py\nx = 1\n```')).toEqual([
      'Name Room',
      'Lab 101',
      'x = 1',
    ]);
  });

  it('tidies syntax a 500-character cut left open', () => {
    expect(markdownToPlainText('Cut **mid [link text](https://exa')).toBe('Cut mid link text');
    expect(markdownToPlainText('# Only a heading')).toBe('Only a heading');
  });

  it('leaves single underscores in words alone', () => {
    expect(markdownToPlainText('the snake_case name')).toBe('the snake_case name');
  });

  it('joins blocks with a separator', () => {
    expect(markdownToPlainText('# Title\n\nBody text')).toBe('Title · Body text');
  });

  it('returns nothing for empty input', () => {
    expect(markdownBlocks('')).toEqual([]);
    expect(markdownBlocks('   \n ')).toEqual([]);
    expect(markdownToPlainText('')).toBe('');
  });

  it('passes plain text through', () => {
    expect(markdownToPlainText('Just a sentence from a PDF.')).toBe('Just a sentence from a PDF.');
  });
});
