/**
 * Unit tests for the MCP Apps sandbox dynamic-CSP CloudFront Function.
 *
 * The function source (`assets/mcp-sandbox/csp-function.js`) is written in
 * the CloudFront Functions JavaScript runtime v2.0 subset, but also
 * exports its pure helpers under `typeof module !== 'undefined'` so we
 * can require it directly from Node. The CDK upload path substitutes the
 * `FRAME_ANCESTORS_PLACEHOLDER` sentinel before the function runs at
 * edge; here we pass `frameAncestors` directly to the builder.
 *
 * Coverage targets:
 *   - Default CSP (no `_meta.ui.csp`) matches the ext-apps basic-host
 *     reference's `buildCspHeader` output. This is what 22/25 reference
 *     example servers run on.
 *   - Declared `connectDomains` / `resourceDomains` / `frameDomains` /
 *     `baseUriDomains` are honored on the right CSP directives.
 *   - Sanitizer rejects every character class the reference rejects
 *     (CSP-injection prevention — this is the security-critical bit).
 *   - The `handler(event)` entry point degrades to default on missing /
 *     malformed input and returns a CloudFront Functions v2.0–shaped
 *     response object.
 */

// eslint-disable-next-line @typescript-eslint/no-var-requires
const {
  sanitizeCspDomains,
  buildCspHeader,
  parseCspParam,
  handler,
} = require('../assets/mcp-sandbox/csp-function');

const FRAME_ANCESTORS = 'https://alpha.example.com';

describe('sanitizeCspDomains', () => {
  test('returns empty array when domains is not an array', () => {
    expect(sanitizeCspDomains(undefined)).toEqual([]);
    expect(sanitizeCspDomains(null)).toEqual([]);
    expect(sanitizeCspDomains('https://example.com')).toEqual([]);
    expect(sanitizeCspDomains({})).toEqual([]);
  });

  test('keeps well-formed origin entries unchanged', () => {
    expect(
      sanitizeCspDomains([
        'https://example.com',
        'https://*.cesium.com',
        'https://esm.sh',
      ]),
    ).toEqual([
      'https://example.com',
      'https://*.cesium.com',
      'https://esm.sh',
    ]);
  });

  test('rejects semicolons (CSP directive break-out)', () => {
    expect(
      sanitizeCspDomains(['https://example.com', "https://evil.com; script-src *"]),
    ).toEqual(['https://example.com']);
  });

  test('rejects newlines (header injection)', () => {
    expect(sanitizeCspDomains(['https://evil.com\nscript-src *'])).toEqual([]);
    expect(sanitizeCspDomains(['https://evil.com\rscript-src *'])).toEqual([]);
  });

  test("rejects quotes (smuggling CSP keywords like 'unsafe-eval')", () => {
    expect(sanitizeCspDomains(["https://evil.com 'unsafe-eval'"])).toEqual([]);
    expect(sanitizeCspDomains(['https://evil.com "x"'])).toEqual([]);
  });

  test('rejects spaces (multi-source smuggling within one entry)', () => {
    expect(sanitizeCspDomains(['https://example.com https://evil.com'])).toEqual(
      [],
    );
  });

  test('rejects non-string entries', () => {
    expect(sanitizeCspDomains(['https://example.com', 42, null, undefined, {}])).toEqual([
      'https://example.com',
    ]);
  });

  test('rejects empty strings', () => {
    expect(sanitizeCspDomains(['', 'https://example.com', ''])).toEqual([
      'https://example.com',
    ]);
  });
});

describe('buildCspHeader — default (no _meta.ui.csp)', () => {
  const csp = buildCspHeader(null, FRAME_ANCESTORS);

  test('matches the ext-apps basic-host reference default tokens', () => {
    // These are the broader-than-spec defaults the upstream reference
    // ships in serve.ts so bundled Apps that omit ui.csp still run.
    // Tightening these would silently break 22/25 of the reference
    // example servers.
    expect(csp).toContain("default-src 'self' 'unsafe-inline'");
    expect(csp).toContain("script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:");
    expect(csp).toContain("style-src 'self' 'unsafe-inline' blob: data:");
    expect(csp).toContain("img-src 'self' data: blob:");
    expect(csp).toContain("font-src 'self' data: blob:");
    expect(csp).toContain("media-src 'self' data: blob:");
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("worker-src 'self' blob:");
  });

  test('locks down un-declared frame / base-uri / object / form-action', () => {
    expect(csp).toContain("frame-src 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("base-uri 'none'");
    expect(csp).toContain("form-action 'none'");
  });

  test('carries frame-ancestors from the synth-time injection', () => {
    expect(csp).toContain('frame-ancestors https://alpha.example.com');
  });

  test('emits directives joined by "; " (no trailing semicolon)', () => {
    expect(csp).toMatch(/^[^;]+(; [^;]+)+$/);
  });
});

describe('buildCspHeader — declared domains', () => {
  test('Excalidraw: connect+resource domains added to all asset directives', () => {
    // From excalidraw/excalidraw-mcp/src/server.ts:
    //   csp: { resourceDomains: ['https://esm.sh'], connectDomains: ['https://esm.sh'] }
    const csp = buildCspHeader(
      {
        resourceDomains: ['https://esm.sh'],
        connectDomains: ['https://esm.sh'],
      },
      FRAME_ANCESTORS,
    );
    expect(csp).toContain(
      "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: https://esm.sh",
    );
    expect(csp).toContain("style-src 'self' 'unsafe-inline' blob: data: https://esm.sh");
    expect(csp).toContain("font-src 'self' data: blob: https://esm.sh");
    expect(csp).toContain("img-src 'self' data: blob: https://esm.sh");
    expect(csp).toContain("media-src 'self' data: blob: https://esm.sh");
    expect(csp).toContain("worker-src 'self' blob: https://esm.sh");
    expect(csp).toContain('connect-src \'self\' https://esm.sh');
  });

  test('CesiumJS map-server: multiple domains on connect-src and resource-* directives', () => {
    // From modelcontextprotocol/ext-apps/examples/map-server/server.ts
    const csp = buildCspHeader(
      {
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
      },
      FRAME_ANCESTORS,
    );
    expect(csp).toContain(
      'connect-src \'self\' https://*.openstreetmap.org https://cesium.com https://*.cesium.com',
    );
    expect(csp).toContain(
      "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: https://*.openstreetmap.org https://cesium.com https://*.cesium.com",
    );
  });

  test('frameDomains: when declared, replaces "frame-src none" — otherwise stays denied', () => {
    const withFrames = buildCspHeader(
      { frameDomains: ['https://youtube.com', 'https://*.youtube.com'] },
      FRAME_ANCESTORS,
    );
    expect(withFrames).toContain('frame-src https://youtube.com https://*.youtube.com');
    expect(withFrames).not.toContain("frame-src 'none'");

    const withoutFrames = buildCspHeader({}, FRAME_ANCESTORS);
    expect(withoutFrames).toContain("frame-src 'none'");
  });

  test('baseUriDomains: when declared, replaces "base-uri none"', () => {
    const withBase = buildCspHeader(
      { baseUriDomains: ['https://example.com'] },
      FRAME_ANCESTORS,
    );
    expect(withBase).toContain('base-uri https://example.com');
    expect(withBase).not.toContain("base-uri 'none'");
  });

  test('connectDomains alone does NOT widen resource-* directives', () => {
    // Spec separation: connectDomains → connect-src only; resourceDomains
    // → script/style/img/font/media/worker. An App that only declares
    // network destinations should not get static-resource permission as
    // a side effect.
    const csp = buildCspHeader(
      { connectDomains: ['https://api.example.com'] },
      FRAME_ANCESTORS,
    );
    expect(csp).toContain('connect-src \'self\' https://api.example.com');
    expect(csp).toContain("script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:");
    expect(csp).not.toMatch(/script-src[^;]*https:\/\/api\.example\.com/);
  });

  test('injection attempts in domain entries are silently dropped (not echoed)', () => {
    const csp = buildCspHeader(
      {
        connectDomains: [
          'https://good.com',
          "https://evil.com; script-src *",
          "https://evil.com 'unsafe-eval'",
          'https://evil.com\nX-Injected: yes',
        ],
      },
      FRAME_ANCESTORS,
    );
    expect(csp).toContain('connect-src \'self\' https://good.com');
    expect(csp).not.toContain('evil.com');
    expect(csp).not.toContain('X-Injected');
    // And the directive separator structure is intact.
    expect(csp).toMatch(/^[^;]+(; [^;]+)+$/);
  });
});

describe('parseCspParam', () => {
  test('null / undefined querystring → null', () => {
    expect(parseCspParam(undefined)).toBeNull();
    expect(parseCspParam(null)).toBeNull();
    expect(parseCspParam({})).toBeNull();
  });

  test('missing csp key → null', () => {
    expect(parseCspParam({ other: { value: 'x' } })).toBeNull();
  });

  test('empty value → null', () => {
    expect(parseCspParam({ csp: { value: '' } })).toBeNull();
  });

  test('valid JSON object → parsed', () => {
    expect(
      parseCspParam({ csp: { value: '{"connectDomains":["https://esm.sh"]}' } }),
    ).toEqual({ connectDomains: ['https://esm.sh'] });
  });

  test('malformed JSON → null (no throw)', () => {
    expect(parseCspParam({ csp: { value: 'not-json' } })).toBeNull();
    expect(parseCspParam({ csp: { value: '{unclosed' } })).toBeNull();
  });

  test('non-object JSON (array / scalar) → null', () => {
    // We accept only the spec-shaped McpUiResourceCsp object — never an
    // array (would let an attacker control directive composition).
    expect(parseCspParam({ csp: { value: '["https://evil.com"]' } })).toBeNull();
    expect(parseCspParam({ csp: { value: '"https://evil.com"' } })).toBeNull();
    expect(parseCspParam({ csp: { value: '42' } })).toBeNull();
    expect(parseCspParam({ csp: { value: 'null' } })).toBeNull();
  });
});

describe('handler', () => {
  function makeEvent(querystring?: Record<string, { value: string }>) {
    return {
      request: { querystring: querystring ?? {} },
      response: { statusCode: 200, headers: {} as Record<string, { value: string }> },
    };
  }

  test('with no ?csp= param, emits the default (un-declared) CSP', () => {
    const event = makeEvent();
    const result = handler(event);
    expect(result.headers['content-security-policy']).toBeDefined();
    expect(result.headers['content-security-policy'].value).toContain(
      "connect-src 'self'",
    );
    // Default → no resource domains beyond keywords/blob/data
    expect(result.headers['content-security-policy'].value).not.toMatch(
      /connect-src 'self' \S+/,
    );
  });

  test('with declared csp, builds and replaces the response CSP header', () => {
    const event = makeEvent({
      csp: {
        value: JSON.stringify({
          resourceDomains: ['https://esm.sh'],
          connectDomains: ['https://esm.sh'],
        }),
      },
    });
    // Pre-existing CSP from ResponseHeadersPolicy is what the CFN
    // overrides — we simulate it being on the response.
    event.response.headers['content-security-policy'] = {
      value: "default-src 'self'",
    };
    const result = handler(event);
    expect(result.headers['content-security-policy'].value).toContain(
      'connect-src \'self\' https://esm.sh',
    );
    expect(result.headers['content-security-policy'].value).not.toBe(
      "default-src 'self'",
    );
  });

  test('with malformed ?csp=, falls back to default without throwing', () => {
    const event = makeEvent({ csp: { value: 'not-json' } });
    const result = handler(event);
    expect(result.headers['content-security-policy'].value).toContain("connect-src 'self'");
    expect(result.headers['content-security-policy'].value).not.toMatch(
      /connect-src 'self' \S/,
    );
  });

  test('always emits frame-ancestors so framing control is not lost on the dynamic path', () => {
    const event = makeEvent();
    const result = handler(event);
    // The placeholder is what's in source; in production CDK substitutes
    // it with the real SPA origin before upload.
    expect(result.headers['content-security-policy'].value).toContain(
      'frame-ancestors __INJECT_FRAME_ANCESTORS__',
    );
  });

  test('returns the response object in the CloudFront Functions v2.0 shape', () => {
    const event = makeEvent({
      csp: { value: JSON.stringify({ connectDomains: ['https://api.example.com'] }) },
    });
    const result = handler(event);
    expect(result).toBe(event.response);
    expect(typeof result.headers['content-security-policy'].value).toBe('string');
  });

  test('handles missing response.headers (defensive)', () => {
    const event = { request: { querystring: {} }, response: {} as any };
    const result = handler(event);
    expect(result.headers).toBeDefined();
    expect(result.headers['content-security-policy']).toBeDefined();
  });
});
