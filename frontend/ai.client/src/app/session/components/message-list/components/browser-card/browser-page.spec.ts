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

function screenshot(id: string, data = 'AAAA'): ToolCallDisplay {
  return call({
    id,
    input: { action: 'screenshot' },
    result: {
      status: 'success',
      content: [{ text: 'Screenshot' }, { image: { format: 'png', data } }],
    },
  });
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
      screenshot('tu-2'),
    ]);

    expect(page?.url).toBe('https://a.test/');
    expect(page?.title).toBe('A');
    expect(page?.screenshot).toBe('data:image/png;base64,AAAA');
  });

  it('drops a screenshot of the page the browser has left', () => {
    const page = browserPageFromCalls([
      call({ result: text('Navigated to https://a.test/\nTitle: A') }),
      screenshot('tu-2'),
      call({ id: 'tu-3', result: text('Navigated to https://b.test/\nTitle: B') }),
    ]);

    expect(page?.url).toBe('https://b.test/');
    expect(page?.screenshot).toBeNull();
  });

  it('reads page text in a navigate result as text, not as a click', () => {
    const page = browserPageFromCalls([
      call({
        result: text('Navigated to https://a.test/\nTitle: A\n\nCurrent URL: https://evil.test/'),
      }),
    ]);

    expect(page?.url).toBe('https://a.test/');
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

describe('browserPageFromCalls across rails', () => {
  // Text the agent writes between calls ends a rail, so navigate and
  // screenshot routinely land in different rails of the same run.
  const navigated = browserPageFromCalls([
    call({ result: text('Navigated to https://a.test/\nTitle: Home') }),
  ]);

  it('puts a later rail\'s screenshot on the page the earlier rail reached', () => {
    const page = browserPageFromCalls([screenshot('tu-2')], navigated);

    expect(page).toEqual({
      url: 'https://a.test/',
      host: 'a.test',
      title: 'Home',
      screenshot: 'data:image/png;base64,AAAA',
      browsing: false,
      closed: false,
    });
  });

  it('shows no card for a screenshot with no page to put it on', () => {
    expect(browserPageFromCalls([screenshot('tu-2')])).toBeNull();
  });

  it('shows no card for a rail that only reads the page', () => {
    for (const action of ['extract_text', 'extract_links', 'evaluate', 'type']) {
      expect(
        browserPageFromCalls(
          [call({ id: 'tu-2', input: { action }, result: text('Current URL: https://b.test/') })],
          navigated,
        ),
      ).toBeNull();
    }
  });

  it('shows no card for a rail that only closes the browser', () => {
    expect(
      browserPageFromCalls(
        [call({ id: 'tu-2', input: { action: 'close' }, result: text('✅ Browser session closed.') })],
        navigated,
      ),
    ).toBeNull();
  });

  it('shows no card for a click that stayed on the page', () => {
    expect(
      browserPageFromCalls(
        [call({ id: 'tu-2', input: { action: 'click', selector: 'button' }, result: text('Clicked. Current URL: https://a.test/') })],
        navigated,
      ),
    ).toBeNull();
  });

  it('shows a card for a click that left the page, without the old title', () => {
    const page = browserPageFromCalls(
      [call({ id: 'tu-2', input: { action: 'click', selector: 'a' }, result: text('Clicked. Current URL: https://a.test/about') })],
      navigated,
    );

    expect(page?.url).toBe('https://a.test/about');
    expect(page?.title).toBeNull();
  });

  it('does not carry a screenshot onto the next page', () => {
    const shot = browserPageFromCalls([screenshot('tu-2')], navigated);
    const page = browserPageFromCalls(
      [call({ id: 'tu-3', result: text('Navigated to https://b.test/\nTitle: B') })],
      shot,
    );

    expect(page?.screenshot).toBeNull();
  });

  it('marks a screenshot rail as browsing only while its own call runs', () => {
    const page = browserPageFromCalls(
      [screenshot('tu-2'), call({ id: 'tu-3', status: 'pending', input: { action: 'extract_text' } })],
      { ...navigated!, browsing: true, closed: true },
    );

    expect(page?.browsing).toBe(true);
    expect(page?.closed).toBe(false);
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
