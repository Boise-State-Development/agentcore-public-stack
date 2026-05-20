/**
 * MCP Apps sandbox — dynamic per-resource Content-Security-Policy.
 *
 * CloudFront Function attached to the proxy.html behavior on
 * **viewer-response**. Reads the `?csp=<urlencoded-json>` query string
 * (where the JSON matches the spec's `McpUiResourceCsp` shape from
 * `_meta.ui.csp`), composes a per-resource CSP header that honors the
 * declared `connectDomains` / `resourceDomains` / `frameDomains` /
 * `baseUriDomains`, and stamps it on the response — replacing any CSP
 * coming from the origin / ResponseHeadersPolicy.
 *
 * Mirrors `modelcontextprotocol/ext-apps/examples/basic-host/serve.ts`'s
 * `buildCspHeader` so a UI resource gets the same CSP from us that it
 * would on the spec's reference host. Failure paths (missing param,
 * malformed JSON, non-object payload) degrade silently to the default
 * (un-declared) CSP — same behavior as if the App had omitted `_meta.ui.csp`.
 *
 * Runtime: CloudFront Functions JavaScript runtime v2.0 (~ES2017 strict
 * subset, sync only, no I/O, no Date.now, no Math.random, no Buffer/URL).
 *
 * Frame-ancestors is the security-critical bit that doesn't vary per
 * resource. It's substituted into this file at CDK synth time by
 * `loadMcpSandboxCspFunctionCode` (lib/mcp-sandbox-stack.ts), which
 * replaces the __INJECT_FRAME_ANCESTORS__ string literal below with a
 * properly JSON-escaped JS literal. The loader asserts the quoted form
 * appears exactly once; this comment uses the bare token so it does not
 * count as a second occurrence. Tests run the file as-is and pass
 * `frameAncestors` directly to `buildCspHeader`.
 *
 * The trailing `if (typeof module !== 'undefined')` block is a no-op in
 * the CloudFront Function runtime (`module` is undeclared, `typeof`
 * returns `'undefined'`) and exposes the pure helpers for Node unit
 * tests in `infrastructure/test/mcp-sandbox-csp-function.test.ts`. Don't
 * delete it — it's how we keep the runtime code and the test surface in
 * the same file with no duplication.
 */
'use strict';

var FRAME_ANCESTORS = '__INJECT_FRAME_ANCESTORS__';

/**
 * Reject domain entries containing CSP-injection characters. Mirrors the
 * upstream reference's regex exactly: `;` / CR / LF break out to a new
 * directive; quotes inject CSP keywords (e.g. `'unsafe-eval'`); space
 * smuggles multiple sources into one entry.
 */
function sanitizeCspDomains(domains) {
  if (!Array.isArray(domains)) return [];
  var out = [];
  for (var i = 0; i < domains.length; i++) {
    var d = domains[i];
    if (typeof d === 'string' && d.length > 0 && !/[;\r\n'" ]/.test(d)) {
      out.push(d);
    }
  }
  return out;
}

/**
 * Compose the CSP header. Defaults mirror the ext-apps basic-host
 * reference (script-src ... 'unsafe-eval' blob: data:), broader than the
 * spec's "Restrictive Default" so bundled Apps that omit `ui.csp` still
 * run. Declared resource/connect/frame/baseUri domains are appended to
 * the corresponding directives.
 *
 * `frame-ancestors`, `form-action`, and `object-src` are security-critical
 * and never vary per resource.
 */
function buildCspHeader(cspConfig, frameAncestors) {
  var csp = cspConfig || {};
  var resourceDomains = sanitizeCspDomains(csp.resourceDomains).join(' ');
  var connectDomains = sanitizeCspDomains(csp.connectDomains).join(' ');
  var frameDomains = sanitizeCspDomains(csp.frameDomains).join(' ');
  var baseUriDomains = sanitizeCspDomains(csp.baseUriDomains).join(' ');

  var directives = [
    "default-src 'self' 'unsafe-inline'",
    ("script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: " + resourceDomains).trim(),
    ("style-src 'self' 'unsafe-inline' blob: data: " + resourceDomains).trim(),
    ("img-src 'self' data: blob: " + resourceDomains).trim(),
    ("font-src 'self' data: blob: " + resourceDomains).trim(),
    ("media-src 'self' data: blob: " + resourceDomains).trim(),
    ("connect-src 'self' " + connectDomains).trim(),
    ("worker-src 'self' blob: " + resourceDomains).trim(),
    frameDomains ? ("frame-src " + frameDomains) : "frame-src 'none'",
    "object-src 'none'",
    baseUriDomains ? ("base-uri " + baseUriDomains) : "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors " + frameAncestors,
  ];

  return directives.join('; ');
}

/**
 * Try to parse a string as a JSON object (not array, not scalar). Returns
 * the parsed object or null. Never throws.
 */
function tryParseObject(s) {
  try {
    var parsed = JSON.parse(s);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed;
    }
    return null;
  } catch (e) {
    return null;
  }
}

/**
 * Extract and parse the `csp` query parameter. CloudFront Functions
 * deliver `request.querystring[name].value` as the parameter value;
 * whether that value is URL-decoded depends on runtime/event-type and
 * doesn't always match the docs in practice. We try the value as-is
 * first, then fall back to an explicit `decodeURIComponent` if that
 * parse fails. Returns the parsed CSP object or null on absent /
 * malformed / non-object input — never throws.
 */
function parseCspParam(querystring) {
  if (!querystring) return null;
  var entry = querystring.csp;
  if (!entry || typeof entry.value !== 'string' || entry.value.length === 0) {
    return null;
  }
  var asIs = tryParseObject(entry.value);
  if (asIs !== null) return asIs;
  var decoded;
  try {
    decoded = decodeURIComponent(entry.value);
  } catch (e) {
    return null;
  }
  if (decoded === entry.value) return null;
  return tryParseObject(decoded);
}

/**
 * TODO(diag): remove after CSP propagation is verified for external Apps.
 * Returns a short diagnostic string describing what `parseCspParam` saw,
 * stamped onto an `x-csp-debug` response header so a curl can pinpoint
 * which branch the function took without redeploying with logs.
 */
function summarizeCspParam(querystring) {
  if (!querystring) return 'no-querystring';
  var entry = querystring.csp;
  if (!entry) return 'no-csp-entry';
  if (typeof entry.value !== 'string') return 'value-not-string';
  if (entry.value.length === 0) return 'empty-value';
  var raw = entry.value;
  var head = raw.slice(0, 60).replace(/[^\x20-\x7e]/g, '?');
  if (tryParseObject(raw) !== null) return 'parsed-raw len=' + raw.length;
  var decoded;
  try {
    decoded = decodeURIComponent(raw);
  } catch (e) {
    return 'decode-threw rawhead=' + head;
  }
  if (decoded === raw) return 'parse-failed-noencoded rawhead=' + head;
  if (tryParseObject(decoded) !== null) return 'parsed-decoded rawlen=' + raw.length;
  return 'parse-failed-both rawhead=' + head;
}

function handler(event) {
  var request = event.request || {};
  var response = event.response || {};
  response.headers = response.headers || {};

  var cspConfig = parseCspParam(request.querystring);
  var csp = buildCspHeader(cspConfig, FRAME_ANCESTORS);

  response.headers['content-security-policy'] = { value: csp };
  // TODO(diag): remove after CSP propagation is verified for external Apps.
  response.headers['x-csp-debug'] = { value: summarizeCspParam(request.querystring) };
  return response;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    sanitizeCspDomains: sanitizeCspDomains,
    buildCspHeader: buildCspHeader,
    parseCspParam: parseCspParam,
    summarizeCspParam: summarizeCspParam,
    handler: handler,
  };
}
