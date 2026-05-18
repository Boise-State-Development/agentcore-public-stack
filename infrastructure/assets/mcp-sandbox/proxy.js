/*
 * MCP Apps host renderer — Sandbox Proxy bootstrap (OUTER iframe).
 *
 * PR #1 of docs/kaizen/scoping/mcp-apps-host-renderer.md. See proxy.html for
 * the architecture overview. This file is intentionally tiny: it exists so
 * the served CSP can be `script-src 'self'` (no inline script, no
 * `'unsafe-inline'`).
 *
 * What it does in PR #1 — and ONLY this:
 *   1. Create the inner content iframe via `srcdoc` (empty placeholder),
 *      establishing the two-iframe Sandbox Proxy structure.
 *   2. Answer a liveness handshake from the embedding SPA so the frontend
 *      (and tests) can prove the cross-origin channel works.
 *
 * What it deliberately does NOT do (PR #4+):
 *   - JSON-RPC 2.0 over postMessage (ui/initialize, ui/notifications/*, etc.)
 *   - Per-frame nonce authentication of the protocol channel
 *   - Loading real MCP App HTML / composing the inner CSP from _meta.ui.csp
 *   - tools/call proxying (PR #5)
 *
 * Origin handling: this outer page can only ever be framed by the SPA origin
 * because CloudFront serves it with `frame-ancestors <SPA origin only>`. The
 * browser enforces that before any script here runs, so the PR #1 handshake
 * does not need to re-check event.origin (and per spec the inner frame's
 * origin will be "null" anyway — real auth is the per-frame nonce added in
 * PR #4). We simply echo back the caller's nonce so the SPA can correlate.
 */
(function () {
  'use strict';

  var PROTOCOL = 'mcp-sandbox-proxy';
  var PHASE = 'pr1-shell';

  // 1. Establish the inner content frame. srcdoc (not src) keeps it at a
  //    distinct opaque origin ("null"); PR #4 replaces this placeholder with
  //    the real MCP App resource and widens `sandbox` per _meta.ui.permissions.
  var inner = document.createElement('iframe');
  inner.id = 'mcp-app-content';
  inner.title = 'MCP App content';
  // Most-restrictive sandbox for the inert placeholder. PR #4 adds
  // allow-scripts / allow-same-origin (spec minimum) and any opted-in
  // capability flags from _meta.ui.permissions.
  inner.setAttribute('sandbox', '');
  inner.setAttribute(
    'srcdoc',
    '<!doctype html><meta charset="utf-8"><title>MCP App placeholder</title>' +
      'MCP Apps sandbox proxy ready (PR #1 shell — no content bound).'
  );
  document.body.appendChild(inner);

  // 2. Liveness handshake. The SPA posts {type:"<PROTOCOL>:ping", nonce} once
  //    its <mcp-app-frame> sees this iframe load; we reply to the sender so
  //    the SPA can confirm the cross-origin proxy is reachable. This is NOT
  //    the protocol channel — that is PR #4.
  window.addEventListener('message', function (event) {
    var data = event && event.data;
    if (!data || data.type !== PROTOCOL + ':ping') {
      return;
    }
    var reply = {
      type: PROTOCOL + ':pong',
      phase: PHASE,
      ready: true,
      // Echo the caller's correlation token verbatim if present.
      nonce: typeof data.nonce === 'string' ? data.nonce : null
    };
    if (event.source && typeof event.source.postMessage === 'function') {
      // targetOrigin "*" is acceptable here: the reply carries no secrets,
      // only a liveness ack, and the embedder is already constrained to the
      // SPA origin by the server-side frame-ancestors CSP.
      event.source.postMessage(reply, '*');
    }
  });

  // Signal to the embedder that the shell finished bootstrapping, for SPAs
  // that prefer to wait for a push rather than poll with a ping.
  if (window.parent && window.parent !== window) {
    window.parent.postMessage(
      { type: PROTOCOL + ':ready', phase: PHASE },
      '*'
    );
  }
})();
