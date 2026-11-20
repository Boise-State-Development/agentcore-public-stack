import { describe, expect, it } from 'vitest';
import { renderIndexedMarkdown, safeHref } from './indexed-markdown';

describe('renderIndexedMarkdown', () => {
  it('renders headings, lists and emphasis', () => {
    const html = renderIndexedMarkdown('# Home - Example\n\n## Programs\n\n- **One**\n- Two\n');
    expect(html).toContain('<h1>Home - Example</h1>');
    expect(html).toContain('<h2>Programs</h2>');
    expect(html).toContain('<li><strong>One</strong></li>');
  });

  it('shows raw block HTML as text, never as markup', () => {
    const html = renderIndexedMarkdown('<script>alert(1)</script>\n\n<div onclick="x()">hi</div>\n');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('<div');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('&lt;div onclick=&quot;x()&quot;&gt;');
  });

  it('shows inline HTML as text', () => {
    const html = renderIndexedMarkdown('before <img src=x onerror=alert(1)> after');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
  });

  it('renders an image as its alt text, so nothing remote is fetched', () => {
    const html = renderIndexedMarkdown('![Campus map](https://tracker.example/pixel.png)');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('tracker.example');
    expect(html).toContain('Campus map');
  });

  it('opens web links in a new tab', () => {
    const html = renderIndexedMarkdown('[Apply](https://www.example.edu/apply "Apply now")');
    expect(html).toContain(
      '<a href="https://www.example.edu/apply" title="Apply now" target="_blank" rel="noopener noreferrer">Apply</a>',
    );
  });

  it('renders a javascript: or relative link as its text', () => {
    const html = renderIndexedMarkdown('[bad](javascript:alert(1)) and [rel](/admin)');
    expect(html).not.toContain('<a');
    expect(html).toContain('bad and rel');
  });

  it('keeps code blocks literal', () => {
    const html = renderIndexedMarkdown('```\n<b>x</b>\n```');
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;');
    expect(html).not.toContain('<b>');
  });
});

describe('safeHref', () => {
  it('allows http, https and mailto only', () => {
    expect(safeHref('https://a.example/x')).toBe('https://a.example/x');
    expect(safeHref('http://a.example/')).toBe('http://a.example/');
    expect(safeHref('mailto:help@example.edu')).toBe('mailto:help@example.edu');
    expect(safeHref('javascript:alert(1)')).toBeNull();
    expect(safeHref('data:text/html,hi')).toBeNull();
    expect(safeHref('/relative')).toBeNull();
    expect(safeHref('')).toBeNull();
    expect(safeHref(null)).toBeNull();
  });
});
