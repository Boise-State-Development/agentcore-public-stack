/**
 * Regression test for the MCP Apps sandbox proxy's INJECTED <meta> CSP
 * (`assets/mcp-sandbox/proxy.js`: `defaultCsp()` / `composeCsp()`).
 *
 * The App View runs under the INTERSECTION of two CSPs: the CloudFront-header
 * CSP (`csp-function.js`) and the `<meta>` CSP `proxy.js` injects into the
 * App HTML. If the `<meta>` omits a `script-src` keyword source the header
 * grants (`'unsafe-eval'`, `blob:`, `data:`), the intersection silently
 * re-denies it — which is exactly what blocked App `eval()` (e.g. Excalidraw)
 * after PR #355 added those sources to the header but not to `proxy.js`.
 *
 * `proxy.js` is a browser IIFE (not Node-requireable), so we assert against
 * its source text. `csp-function.js` exports `buildCspHeader`, so we derive
 * the header's keyword sources programmatically rather than hard-coding them
 * — the test then fails if the two sources drift apart again.
 */
import * as fs from 'fs';
import * as path from 'path';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { buildCspHeader } = require('../assets/mcp-sandbox/csp-function');

const PROXY_SRC = fs.readFileSync(
  path.join(__dirname, '../assets/mcp-sandbox/proxy.js'),
  'utf8',
);

// The two `script-src` directive literals proxy.js builds (one in
// defaultCsp(), one in composeCsp()). Match on the literal start so prose
// comments mentioning "script-src" can't false-match.
const proxyScriptSrcDirectives = PROXY_SRC.split('\n').filter((line) =>
  line.includes("script-src 'self'"),
);

// Keyword sources (quoted keywords like 'unsafe-eval' + scheme sources like
// blob:) on a CSP's script-src — these are what an intersection can strip.
// Domains and 'self'/'unsafe-inline' are shared by both policies; the drift
// that bit us was the keyword sources.
function scriptSrcKeywordSources(csp: string): string[] {
  const directive = csp
    .split(';')
    .map((d) => d.trim())
    .find((d) => d.startsWith('script-src'));
  if (!directive) return [];
  return directive
    .split(/\s+/)
    .slice(1) // drop the "script-src" directive name
    .filter((tok) => /^'[^']+'$/.test(tok) || /^[a-z]+:$/.test(tok));
}

describe('mcp-sandbox proxy.js injected <meta> CSP — no drift vs header', () => {
  const headerKeywords = scriptSrcKeywordSources(
    buildCspHeader({}, 'https://x.example'),
  );

  test('header (source of truth) grants unsafe-eval / blob: / data:', () => {
    expect(headerKeywords).toEqual(
      expect.arrayContaining(["'unsafe-eval'", 'blob:', 'data:']),
    );
  });

  test('proxy.js builds two script-src directives (defaultCsp + composeCsp)', () => {
    expect(proxyScriptSrcDirectives).toHaveLength(2);
  });

  test.each(["'unsafe-eval'", 'blob:', 'data:'])(
    'every proxy.js script-src directive grants %s',
    (token) => {
      for (const directive of proxyScriptSrcDirectives) {
        expect(directive).toContain(token);
      }
    },
  );

  test('every header script-src keyword source also appears in proxy.js', () => {
    // The anti-drift guard: a keyword the header grants but proxy.js omits
    // would be silently stripped by the intersection.
    for (const keyword of headerKeywords) {
      for (const directive of proxyScriptSrcDirectives) {
        expect(directive).toContain(keyword);
      }
    }
  });
});
