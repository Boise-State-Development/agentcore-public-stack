/*
 * MCP Apps host renderer — Sandbox Proxy (OUTER iframe).
 *
 * PR #4 of docs/kaizen/scoping/mcp-apps-host-renderer.md (supersedes the
 * PR #1 liveness shell). Normative spec: ext-apps
 * specification/2026-01-26/apps.mdx, "Sandbox proxy".
 *
 * This file is served from the dedicated mcp-sandbox origin and runs inside
 * the OUTER iframe the SPA created with sandbox="allow-scripts
 * allow-same-origin". It is the stable cross-origin boundary between the
 * host (ai.client) and the untrusted App View (an inner null-origin srcdoc
 * iframe this script creates).
 *
 * Responsibilities (spec §"Sandbox proxy"):
 *   3. Announce readiness to the host (ui/notifications/sandbox-proxy-ready).
 *   4. Receive the raw HTML + sandbox/CSP/permissions
 *      (ui/notifications/sandbox-resource-ready).
 *   5. Load the View HTML in the inner iframe with a CSP composed from
 *      _meta.ui.csp plus the spec's deny-by-default fallbacks; map
 *      _meta.ui.permissions onto the inner iframe `allow` attribute.
 *   6. Forward every JSON-RPC message host<->View whose method does not
 *      start with "ui/notifications/sandbox-". The host enforces the
 *      "no sends before initialized" rule; the proxy is a dumb pipe.
 *
 * Auth: a per-frame nonce, minted by the host and delivered in
 * sandbox-resource-ready, authenticates the host<->proxy leg (the inner
 * View is null-origin, so origin matching is impossible — the nonce is the
 * real check the spec mandates). The proxy adds the nonce on View->host
 * forwards and strips it on host->View forwards (the View speaks plain
 * spec JSON-RPC and never sees transport auth). No inline script: the
 * served CSP can stay script-src 'self'.
 */
(function () {
  'use strict';

  var PROXY_READY = 'ui/notifications/sandbox-proxy-ready';
  var RESOURCE_READY = 'ui/notifications/sandbox-resource-ready';
  var SANDBOX_RESERVED_PREFIX = 'ui/notifications/sandbox-';

  var hostWindow = window.parent;
  var hostOrigin = null; // learned from the first sandbox-resource-ready
  var nonce = null;
  var inner = null;
  var innerReady = false;
  var pendingToInner = []; // host->View messages queued until inner loads

  // --- CSP composition (spec §"Sandbox proxy" point 5 + Host Behavior) ----

  function list(domains) {
    return Array.isArray(domains)
      ? domains.filter(function (d) {
          return typeof d === 'string' && d.length > 0;
        })
      : [];
  }

  // Restrictive default when no _meta.ui.csp is supplied (verbatim from the
  // normative spec), hardened with object-src/frame-src/base-uri.
  function defaultCsp() {
    return [
      "default-src 'none'",
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "media-src 'self' data:",
      "font-src 'self'",
      "connect-src 'none'",
      "frame-src 'none'",
      "base-uri 'self'",
      "object-src 'none'",
      "form-action 'none'"
    ].join('; ');
  }

  // Compose from declared domains. resourceDomains maps to the static
  // resource directives; connectDomains to connect-src; frameDomains to
  // frame-src; baseUriDomains to base-uri. Undeclared => deny (spec: MUST
  // NOT allow undeclared domains; MAY further restrict).
  function composeCsp(csp) {
    if (!csp || typeof csp !== 'object') {
      return defaultCsp();
    }
    var res = list(csp.resourceDomains).join(' ');
    var conn = list(csp.connectDomains).join(' ');
    var frame = list(csp.frameDomains).join(' ');
    var base = list(csp.baseUriDomains).join(' ');
    return [
      "default-src 'none'",
      ("script-src 'self' 'unsafe-inline'" + (res ? ' ' + res : '')),
      ("style-src 'self' 'unsafe-inline'" + (res ? ' ' + res : '')),
      ("img-src 'self' data:" + (res ? ' ' + res : '')),
      ("font-src 'self'" + (res ? ' ' + res : '')),
      ("media-src 'self' data:" + (res ? ' ' + res : '')),
      ('connect-src ' + (conn || "'none'")),
      ('frame-src ' + (frame || "'none'")),
      ('base-uri ' + (base || "'self'")),
      "object-src 'none'",
      "form-action 'none'"
    ].join('; ');
  }

  // Map _meta.ui.permissions (object form, SEP-1865) to a Permissions-Policy
  // `allow` attribute value for the inner iframe.
  function allowAttr(permissions) {
    if (!permissions || typeof permissions !== 'object') {
      return '';
    }
    var feats = [];
    if (permissions.camera) feats.push('camera');
    if (permissions.microphone) feats.push('microphone');
    if (permissions.geolocation) feats.push('geolocation');
    if (permissions.clipboardWrite) feats.push('clipboard-write');
    return feats.join('; ');
  }

  // Inject the composed CSP as the first <head> child so it governs the
  // whole document. Relies on the App being a valid HTML5 document (spec
  // MUST); falls back to wrapping if no <head> is present.
  function withCsp(html, cspValue) {
    var meta =
      '<meta http-equiv="Content-Security-Policy" content="' +
      cspValue.replace(/"/g, '&quot;') +
      '">';
    if (/<head[^>]*>/i.test(html)) {
      return html.replace(/(<head[^>]*>)/i, '$1' + meta);
    }
    if (/<html[^>]*>/i.test(html)) {
      return html.replace(/(<html[^>]*>)/i, '$1<head>' + meta + '</head>');
    }
    return '<!doctype html><html><head>' + meta + '</head><body>' + html +
      '</body></html>';
  }

  // --- inner iframe (the View) -------------------------------------------

  function mountView(params) {
    var sandbox =
      typeof params.sandbox === 'string' && params.sandbox
        ? params.sandbox
        : 'allow-scripts';
    var allow = allowAttr(params.permissions);

    inner = document.createElement('iframe');
    inner.id = 'mcp-app-content';
    inner.title = 'MCP App content';
    inner.setAttribute('sandbox', sandbox);
    if (allow) {
      inner.setAttribute('allow', allow);
    }
    inner.setAttribute('referrerpolicy', 'no-referrer');
    inner.style.cssText =
      'border:0;width:100%;height:100%;display:block;background:#fff';
    // Build the App document and hand it to the inner iframe as a blob URL.
    // srcdoc / data: / about:blank are "local schemes" that inherit the
    // embedder's HTTP CSP (CSP3 §"Initialize a Document's CSP list"); blob:
    // is NOT, so the inner doc's effective CSP is exactly what composeCsp
    // emits via the injected <meta> tag — no intersection with proxy.html's
    // strict `script-src 'self'`. Null-origin is unaffected: it comes from
    // `sandbox` without `allow-same-origin`, regardless of URL scheme. The
    // per-frame nonce is still the real channel auth.
    var blob = new Blob(
      [withCsp(String(params.html || ''), composeCsp(params.csp))],
      { type: 'text/html' }
    );
    var blobUrl = URL.createObjectURL(blob);
    inner.addEventListener('load', function () {
      // Release the blob backing store as soon as the doc is loaded —
      // keeping it would pin memory for the iframe's lifetime.
      URL.revokeObjectURL(blobUrl);
      innerReady = true;
      var queued = pendingToInner.splice(0, pendingToInner.length);
      for (var i = 0; i < queued.length; i++) {
        postToInner(queued[i]);
      }
    });
    inner.src = blobUrl;
    document.body.appendChild(inner);
  }

  // --- message plumbing ---------------------------------------------------

  function isJsonRpc(d) {
    return d && typeof d === 'object' && d.jsonrpc === '2.0';
  }

  function methodOf(d) {
    return d && typeof d.method === 'string' ? d.method : null;
  }

  function isSandboxReserved(method) {
    return !!method && method.indexOf(SANDBOX_RESERVED_PREFIX) === 0;
  }

  function postToInner(msg) {
    if (!inner || !inner.contentWindow) {
      return;
    }
    // Inner is null-origin; targetOrigin must be "*". Strip transport nonce
    // so the View only ever sees spec-clean JSON-RPC.
    var clean = {};
    for (var k in msg) {
      if (Object.prototype.hasOwnProperty.call(msg, k) && k !== 'nonce') {
        clean[k] = msg[k];
      }
    }
    inner.contentWindow.postMessage(clean, '*');
  }

  function postToHost(msg) {
    if (!hostWindow) {
      return;
    }
    var withNonce = {};
    for (var k in msg) {
      if (Object.prototype.hasOwnProperty.call(msg, k)) {
        withNonce[k] = msg[k];
      }
    }
    if (nonce) {
      withNonce.nonce = nonce;
    }
    hostWindow.postMessage(withNonce, hostOrigin || '*');
  }

  function onHostMessage(event) {
    var data = event.data;
    if (!isJsonRpc(data)) {
      return;
    }
    var method = methodOf(data);

    if (method === RESOURCE_READY) {
      // First authenticated host message: lock onto the host origin and
      // the per-frame nonce, then mount the View.
      if (inner) {
        return; // one resource per proxy instance
      }
      hostOrigin = event.origin && event.origin !== 'null' ? event.origin : null;
      nonce =
        data.params && typeof data.params.nonce === 'string'
          ? data.params.nonce
          : null;
      mountView(data.params || {});
      return;
    }

    // Reserved sandbox-* messages are proxy-private and never forwarded.
    if (isSandboxReserved(method)) {
      return;
    }

    // Everything else is host->View. Authenticate the nonce once armed.
    if (nonce && data.nonce !== nonce) {
      return;
    }
    if (innerReady) {
      postToInner(data);
    } else {
      pendingToInner.push(data);
    }
  }

  function onInnerMessage(event) {
    if (!inner || event.source !== inner.contentWindow) {
      return;
    }
    var data = event.data;
    if (!isJsonRpc(data)) {
      return;
    }
    // The View must not speak the reserved sandbox channel.
    if (isSandboxReserved(methodOf(data))) {
      return;
    }
    postToHost(data);
  }

  window.addEventListener('message', function (event) {
    if (event.source === hostWindow) {
      onHostMessage(event);
    } else if (inner && event.source === inner.contentWindow) {
      onInnerMessage(event);
    }
  });

  // Step 3: announce readiness. targetOrigin "*" is acceptable — this
  // carries no secret and the host validates by source window + origin
  // before sending the nonce-bearing resource.
  if (hostWindow && hostWindow !== window) {
    hostWindow.postMessage({ jsonrpc: '2.0', method: PROXY_READY, params: {} }, '*');
  }
})();
