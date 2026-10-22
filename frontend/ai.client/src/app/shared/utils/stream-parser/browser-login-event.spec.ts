import { describe, it, expect, vi } from 'vitest';

import {
  processStreamEvent,
  validateBrowserLoginRequiredEvent,
  StreamParserCallbacks,
} from './stream-parser-core';

function event(overrides: Record<string, unknown> = {}) {
  return {
    type: 'browser_login_required',
    interruptId: 'v1:tool_call:tu-1:abc',
    toolUseId: 'tu-1',
    sessionId: 'conv-1',
    browserSessionId: 'bs-1',
    browserId: 'browser-abc',
    viewport: { width: 1280, height: 800 },
    deadlineAt: '2026-09-18T12:08:00+00:00',
    targetUrl: 'https://www.jstor.org/action/showLogin',
    reason: 'Sign in to JSTOR so I can read the results page.',
    ...overrides,
  };
}

describe('validateBrowserLoginRequiredEvent', () => {
  it('accepts a well-formed handover', () => {
    expect(validateBrowserLoginRequiredEvent(event())).toBe(true);
  });

  it('accepts one with only the required fields', () => {
    expect(
      validateBrowserLoginRequiredEvent(
        event({ deadlineAt: undefined, targetUrl: undefined, reason: undefined }),
      ),
    ).toBe(true);
  });

  it.each([
    ['a missing interrupt id', { interruptId: undefined }],
    ['an empty interrupt id', { interruptId: '' }],
    ['a missing browser session', { browserSessionId: undefined }],
    ['an empty browser session', { browserSessionId: '' }],
    ['a missing browser id', { browserId: '' }],
    ['a missing conversation id', { sessionId: undefined }],
    ['the wrong type', { type: 'user_question_required' }],
  ])('rejects %s', (_label, overrides) => {
    expect(validateBrowserLoginRequiredEvent(event(overrides))).toBe(false);
  });

  it.each([
    ['a missing viewport', undefined],
    ['a partial viewport', { width: 1280 }],
    ['a zero viewport', { width: 0, height: 800 }],
    ['a string viewport', { width: '1280', height: '800' }],
  ])('rejects %s, because DCV would silently crop', (_label, viewport) => {
    expect(validateBrowserLoginRequiredEvent(event({ viewport }))).toBe(false);
  });

  it('rejects an event carrying a live-view url', () => {
    // Nothing upstream should ever put one here — the backend asserts it too.
    // If one appears, a contract regression shipped, and refusing to render is
    // better than framing a URL of unknown provenance.
    expect(
      validateBrowserLoginRequiredEvent(
        event({ url: 'https://example.com/live?X-Amz-Signature=deadbeef' }),
      ),
    ).toBe(false);
    expect(
      validateBrowserLoginRequiredEvent(
        event({ liveViewUrl: 'https://example.com/live' }),
      ),
    ).toBe(false);
  });

  it.each([null, undefined, 'string', 42, []])('rejects %s', (data) => {
    expect(validateBrowserLoginRequiredEvent(data)).toBe(false);
  });
});

describe('processStreamEvent: browser_login_required', () => {
  it('routes a valid event to the callback', () => {
    const onBrowserLoginRequired = vi.fn();
    const callbacks: StreamParserCallbacks = { onBrowserLoginRequired };

    processStreamEvent('browser_login_required', event(), callbacks);

    expect(onBrowserLoginRequired).toHaveBeenCalledOnce();
    expect(onBrowserLoginRequired.mock.calls[0][0].browserSessionId).toBe('bs-1');
  });

  it('reports a malformed event as a parse error instead of rendering it', () => {
    const onBrowserLoginRequired = vi.fn();
    const onParseError = vi.fn();

    processStreamEvent(
      'browser_login_required',
      event({ viewport: undefined }),
      { onBrowserLoginRequired, onParseError },
    );

    expect(onBrowserLoginRequired).not.toHaveBeenCalled();
    expect(onParseError).toHaveBeenCalledWith(
      'browser_login_required: invalid data structure',
    );
  });

  it('is inert when no handler is registered', () => {
    // An environment running the backend without the viewer must not throw on
    // an event it has no renderer for.
    expect(() =>
      processStreamEvent('browser_login_required', event(), {}),
    ).not.toThrow();
  });
});
