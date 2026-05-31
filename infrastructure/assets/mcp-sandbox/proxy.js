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
 * host (ai.client) and the untrusted App View (an inner iframe this script
 * creates, mounted via document.write per the ext-apps basic-host
 * reference). The inner iframe defaults to allow-scripts +
 * allow-same-origin + allow-forms (matches the basic-host reference) so
 * document.write can populate it and typical App bundles can use
 * localStorage at the sandbox origin; the App may override its
 * `_meta.ui.sandbox` to opt back into null-origin (we'll fall back to
 * srcdoc when contentDocument isn't writable).
 *
 * Responsibilities (spec §"Sandbox proxy"):
 *   3. Announce readiness to the host (ui/notifications/sandbox-proxy-ready).
 *   4. Receive the raw HTML + sandbox/CSP/permissions
 *      (ui/notifications/sandbox-resource-ready).
 *   5. Load the View HTML in the inner iframe via document.write (falls
 *      back to srcdoc if the App opted into a stricter cross-origin
 *      sandbox). Inject a per-resource <meta> CSP composed from
 *      _meta.ui.csp; map _meta.ui.permissions onto the inner iframe's
 *      `allow` attribute. NOTE: the inner doc also INHERITS this proxy's
 *      HTTP CSP (CSP3 local-scheme rule applies to srcdoc + document.write-
 *      populated about:blank alike); that inherited policy — set in
 *      mcp-sandbox-stack.ts — is the load-bearing security bound. The
 *      injected <meta> can only further-restrict via intersection.
 *   6. Forward every JSON-RPC message host<->View whose method does not
 *      start with "ui/notifications/sandbox-". The host enforces the
 *      "no sends before initialized" rule; the proxy is a dumb pipe.
 *
 * Auth: a per-frame nonce, minted by the host and delivered in
 * sandbox-resource-ready, authenticates the host<->proxy leg. The proxy
 * adds the nonce on View->host forwards and strips it on host->View
 * forwards (the View speaks plain spec JSON-RPC and never sees transport
 * auth). proxy.html itself ships zero inline content so 'unsafe-inline'
 * on the inherited CSP can't be exploited against the shell.
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

  // Establish a 100%-height chain on this shell so the inner View iframe
  // (height:100%, set in mountView) fills the OUTER iframe the host sized
  // instead of collapsing to the CSS default replaced-element height
  // (150px) — a percentage height resolves to `auto` when no ancestor has a
  // resolved height. proxy.html ships zero inline styles to keep its served
  // CSP posture tight; the CSSOM `.style` path here isn't governed by the
  // `style-src` directive, so this is the CSP-safe place to do it.
  var docEl = document.documentElement;
  if (docEl && docEl.style) {
    docEl.style.height = '100%';
  }
  if (document.body && document.body.style) {
    document.body.style.height = '100%';
    document.body.style.margin = '0';
    document.body.style.overflow = 'hidden';
  }

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
      // Keyword sources ('unsafe-eval' blob: data:) MUST match the
      // CloudFront-header CSP (assets/mcp-sandbox/csp-function.js). The
      // browser enforces the INTERSECTION of this injected <meta> and that
      // header, so any source the meta omits is silently re-denied even
      // though the header allows it — that drift is what blocked App `eval`.
      "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:",
      "style-src 'self' 'unsafe-inline' blob: data:",
      "img-src 'self' data: blob:",
      "media-src 'self' data: blob:",
      "font-src 'self' data: blob:",
      "connect-src 'none'",
      "worker-src 'self' blob:",
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
      // Keyword sources ('unsafe-eval' blob: data:) MUST match the
      // CloudFront-header CSP (assets/mcp-sandbox/csp-function.js). The
      // browser enforces the INTERSECTION of this <meta> and that header, so
      // a source omitted here is silently re-denied regardless of the header
      // — declared resourceDomains are appended on top, as before.
      ("script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:" + (res ? ' ' + res : '')),
      ("style-src 'self' 'unsafe-inline' blob: data:" + (res ? ' ' + res : '')),
      ("img-src 'self' data: blob:" + (res ? ' ' + res : '')),
      ("font-src 'self' data: blob:" + (res ? ' ' + res : '')),
      ("media-src 'self' data: blob:" + (res ? ' ' + res : '')),
      ('connect-src ' + (conn || "'none'")),
      ("worker-src 'self' blob:" + (res ? ' ' + res : '')),
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
    // Default matches the ext-apps basic-host reference
    // (`examples/basic-host/src/sandbox.ts`): allow-scripts +
    // allow-same-origin + allow-forms. allow-same-origin lets document.write
    // populate the inner doc (contentDocument is only accessible when the
    // inner is same-origin to this proxy — fine, the proxy origin is a
    // static CDN with no shared state) and lets typical bundled Apps reach
    // localStorage at the sandbox origin. The App can override via
    // `_meta.ui.sandbox` to opt back into null-origin for stricter isolation;
    // we'll fall back to srcdoc when contentDocument isn't writable.
    var sandbox =
      typeof params.sandbox === 'string' && params.sandbox
        ? params.sandbox
        : 'allow-scripts allow-same-origin allow-forms';
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
    inner.addEventListener('load', function () {
      innerReady = true;
      var queued = pendingToInner.splice(0, pendingToInner.length);
      for (var i = 0; i < queued.length; i++) {
        postToInner(queued[i]);
      }
    });
    document.body.appendChild(inner);

    // Build the App document. Per the ext-apps basic-host reference
    // (examples/basic-host/src/sandbox.ts): "Use document.write instead
    // of srcdoc (which the CesiumJS Map won't work with)". The inner
    // document inherits this proxy's HTTP CSP either way — that's the
    // load-bearing security boundary (see buildMcpSandboxProxyCsp in
    // mcp-sandbox-stack.ts). The per-App CSP meta tag we still inject
    // *intersects* the inherited policy, so Apps can further restrict
    // but not loosen. Per-frame nonce is the channel auth.
    var html = withCsp(String(params.html || ''), composeCsp(params.csp));
    var doc = null;
    try {
      doc = inner.contentDocument || (inner.contentWindow && inner.contentWindow.document);
    } catch (_) {
      // Cross-origin access throws; we'll fall back to srcdoc below.
      doc = null;
    }
    if (doc) {
      doc.open();
      doc.write(html);
      doc.close();
    } else {
      // Fallback path: the App opted into a stricter sandbox without
      // allow-same-origin, so contentDocument is cross-origin. srcdoc
      // works for the vast majority of Apps; CesiumJS-class outliers
      // would need to relax their declared sandbox.
      inner.setAttribute('srcdoc', html);
    }
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
