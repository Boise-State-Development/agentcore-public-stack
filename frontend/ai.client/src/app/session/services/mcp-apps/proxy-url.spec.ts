import { describe, expect, test } from 'vitest';
import { buildProxyUrl } from './proxy-url';

describe('buildProxyUrl', () => {
  const ORIGIN = 'https://mcp-sandbox.example.com';

  test('no csp at all → bare proxy.html (matches the default-CSP fast path)', () => {
    expect(buildProxyUrl(ORIGIN, null)).toBe(
      'https://mcp-sandbox.example.com/proxy.html',
    );
    expect(buildProxyUrl(ORIGIN, undefined)).toBe(
      'https://mcp-sandbox.example.com/proxy.html',
    );
    expect(buildProxyUrl(ORIGIN, {})).toBe(
      'https://mcp-sandbox.example.com/proxy.html',
    );
  });

  test('declared-but-empty arrays → bare proxy.html (no ?csp=)', () => {
    // The CFN would produce the default CSP for any of these — sending
    // ?csp= would just bust the CloudFront cache for no gain.
    expect(
      buildProxyUrl(ORIGIN, {
        connectDomains: [],
        resourceDomains: [],
        frameDomains: [],
        baseUriDomains: [],
      }),
    ).toBe('https://mcp-sandbox.example.com/proxy.html');
  });

  test('Excalidraw shape: connect+resource domains → ?csp= present and JSON-encoded', () => {
    const url = buildProxyUrl(ORIGIN, {
      connectDomains: ['https://esm.sh'],
      resourceDomains: ['https://esm.sh'],
    });
    expect(url.startsWith('https://mcp-sandbox.example.com/proxy.html?csp=')).toBe(true);
    const encoded = url.split('?csp=')[1];
    const decoded = JSON.parse(decodeURIComponent(encoded));
    expect(decoded).toEqual({
      connectDomains: ['https://esm.sh'],
      resourceDomains: ['https://esm.sh'],
    });
  });

  test('CesiumJS map-server shape: multi-entry arrays survive round-trip intact', () => {
    const csp = {
      connectDomains: [
        'https://*.openstreetmap.org',
        'https://cesium.com',
        'https://*.cesium.com',
      ],
      resourceDomains: [
        'https://*.openstreetmap.org',
        'https://cesium.com',
        'https://*.cesium.com',
      ],
    };
    const url = buildProxyUrl(ORIGIN, csp);
    const encoded = url.split('?csp=')[1];
    expect(JSON.parse(decodeURIComponent(encoded))).toEqual(csp);
  });

  test('any single declared array suffices to attach ?csp=', () => {
    expect(buildProxyUrl(ORIGIN, { frameDomains: ['https://x.test'] })).toContain('?csp=');
    expect(buildProxyUrl(ORIGIN, { baseUriDomains: ['https://x.test'] })).toContain(
      '?csp=',
    );
    expect(buildProxyUrl(ORIGIN, { connectDomains: ['https://x.test'] })).toContain(
      '?csp=',
    );
    expect(buildProxyUrl(ORIGIN, { resourceDomains: ['https://x.test'] })).toContain(
      '?csp=',
    );
  });

  test('trailing slash on origin is stripped (no double slash)', () => {
    expect(buildProxyUrl('https://mcp-sandbox.example.com/', null)).toBe(
      'https://mcp-sandbox.example.com/proxy.html',
    );
    expect(
      buildProxyUrl('https://mcp-sandbox.example.com/', {
        connectDomains: ['https://esm.sh'],
      }),
    ).toBe(
      'https://mcp-sandbox.example.com/proxy.html?csp=' +
        encodeURIComponent('{"connectDomains":["https://esm.sh"]}'),
    );
  });

  test('non-object csp inputs degrade safely', () => {
    // Defensive: SSE event payloads come from a typed channel but JS lets
    // anything through at runtime. Anything not an object → no ?csp.
    expect(buildProxyUrl(ORIGIN, 'not-an-object' as unknown as null)).toBe(
      'https://mcp-sandbox.example.com/proxy.html',
    );
    expect(buildProxyUrl(ORIGIN, 42 as unknown as null)).toBe(
      'https://mcp-sandbox.example.com/proxy.html',
    );
  });

  test('encodes URL-special characters in domain entries', () => {
    // The query value is the JSON serialization, then percent-encoded —
    // so a `&` or `=` inside a domain entry (shouldn't happen, but still)
    // gets encoded and can't break out of the query param.
    const url = buildProxyUrl(ORIGIN, {
      connectDomains: ['https://api.example.com?token=abc&other=xyz'],
    });
    const encoded = url.split('?csp=')[1];
    // The encoded value contains neither a bare `&` nor a bare `=` — both
    // would be percent-encoded (`%26`, `%3D`).
    expect(encoded).not.toMatch(/&/);
    expect(encoded.split('=').length).toBe(1); // no literal `=` in the encoded value
    expect(JSON.parse(decodeURIComponent(encoded))).toEqual({
      connectDomains: ['https://api.example.com?token=abc&other=xyz'],
    });
  });
});
