import { describe, it, expect } from 'vitest';

import { browserPageFromCalls, hostLabel } from './browser-page';
import type { ToolCallDisplay } from '../tool-rail/tool-rail.model';

function call(overrides: Partial<ToolCallDisplay>): ToolCallDisplay {
  return {
    id: 'tu-1',
    toolName: 'browse_web',
    input: { action: 'navigate', url: 'https://example.com' },
    status: 'complete',
    ...overrides,
  };
}

function text(value: string): ToolCallDisplay['result'] {
  return { status: 'success', content: [{ text: value }] };
}

describe('browserPageFromCalls', () => {
  it('reads the URL and title from a navigate result', () => {
    const page = browserPageFromCalls([
      call({
        result: text('✅ Navigated to https://www.boisestate.edu/\nTitle: Boise State University\n\nbody'),
      }),
    ]);

    expect(page).toEqual({
      url: 'https://www.boisestate.edu/',
      host: 'boisestate.edu',
      title: 'Boise State University',
      screenshot: null,
      browsing: false,
      closed: false,
    });
  });

  it('ignores every other tool', () => {
    expect(
      browserPageFromCalls([call({ toolName: 'fetch_url_content', result: text('Navigated to https://a.test\nTitle: A') })]),
    ).toBeNull();
  });

  it('follows a click to a new page and drops the stale title', () => {
    const page = browserPageFromCalls([
      call({ result: text('Navigated to https://a.test/\nTitle: Home') }),
      call({
        id: 'tu-2',
        input: { action: 'click', selector: 'a' },
        result: text('✅ Clicked `a`. Current URL: https://a.test/about'),
      }),
    ]);

    expect(page?.url).toBe('https://a.test/about');
    expect(page?.title).toBeNull();
  });

  it('uses the latest screenshot as the thumbnail', () => {
    const page = browserPageFromCalls([
      call({ result: text('Navigated to https://a.test/\nTitle: A') }),
      call({
        id: 'tu-2',
        input: { action: 'screenshot' },
        result: {
          status: 'success',
          content: [{ text: 'Screenshot' }, { image: { format: 'png', data: 'AAAA' } }],
        },
      }),
    ]);

    expect(page?.screenshot).toBe('data:image/png;base64,AAAA');
  });

  it('shows the target of a navigate still running', () => {
    const page = browserPageFromCalls([
      call({ status: 'pending', input: { action: 'navigate', url: 'https://b.test/x' } }),
    ]);

    expect(page?.url).toBe('https://b.test/x');
    expect(page?.browsing).toBe(true);
  });

  it('refuses non-http URLs from the page', () => {
    // The URL comes from `location.href` on a remote page; it must never
    // become a javascript: link in the conversation.
    expect(
      browserPageFromCalls([call({ result: text('Navigated to javascript:alert(1)\nTitle: x') })]),
    ).toBeNull();
  });

  it('notes when the agent closed the browser at the end', () => {
    const page = browserPageFromCalls([
      call({ result: text('Navigated to https://a.test/\nTitle: A') }),
      call({ id: 'tu-2', input: { action: 'close' }, result: text('✅ Browser session closed.') }),
    ]);

    expect(page?.closed).toBe(true);
  });
});

describe('hostLabel', () => {
  it('drops a leading www', () => {
    expect(hostLabel('https://www.jstor.org/action')).toBe('jstor.org');
  });

  it('returns null for an unparseable value', () => {
    expect(hostLabel('not a url')).toBeNull();
  });
});
