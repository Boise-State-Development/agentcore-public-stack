import { describe, it, expect } from 'vitest';
import { DomSanitizer } from '@angular/platform-browser';
import { JsonSyntaxHighlightPipe } from './json-syntax-highlight.pipe';

const passthrough = { bypassSecurityTrustHtml: (html: string) => html } as unknown as DomSanitizer;

function highlight(value: unknown): string {
  return new JsonSyntaxHighlightPipe(passthrough).transform(
    JSON.stringify(value, null, 2),
  ) as string;
}

function text(html: string): string {
  return html.replace(/<[^>]+>/g, '');
}

describe('JsonSyntaxHighlightPipe', () => {
  it('leaves the text of string values untouched', () => {
    // The chained-replace version rewrote "09:30:17" as "09: 30: 17".
    const value = { current_time: '2026-09-30T09:30:17-06:00', note: 'a: true, b: null {x}' };
    expect(text(highlight(value))).toBe(JSON.stringify(value, null, 2));
  });

  it('classifies keys, strings, numbers, booleans, null and brackets', () => {
    const html = highlight({ k: 'v', n: -1.5, t: true, z: null, a: [1] });
    expect(html).toContain('<span class="json-key">"k"</span>:');
    expect(html).toContain('<span class="json-string">"v"</span>');
    expect(html).toContain('<span class="json-number">-1.5</span>');
    expect(html).toContain('<span class="json-boolean">true</span>');
    expect(html).toContain('<span class="json-null">null</span>');
    expect(html).toContain('<span class="json-bracket">[</span>');
  });

  it('escapes HTML inside values', () => {
    expect(highlight({ x: '<img src=x>' })).toContain('&lt;img src=x&gt;');
  });

  it('keeps escaped quotes inside a string', () => {
    const value = { q: 'say "hi": 5' };
    expect(text(highlight(value))).toBe(JSON.stringify(value, null, 2));
  });
});
