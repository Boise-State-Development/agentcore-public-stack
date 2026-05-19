# Scoping — MCP Sandbox Dynamic Per-Resource CSP

> Status: Shipping — feature/mcp-sandbox-dynamic-csp
> Owner: Phil Merrell
> Source: dogfood gotcha #3 in [[project-mcp-apps-pr-progress]] (Option 3 of the host-renderer CSP fix); follow-up to #353
> Spec read: draft `specification/draft/apps.mdx` lines 283–296; reference implementation `modelcontextprotocol/ext-apps/examples/basic-host/serve.ts`

## TL;DR — **Ship**

PR #353 shipped Options 1+2 (broad static outer CSP + `document.write()` mount). That works for the 22/25 reference servers that don't declare `_meta.ui.csp`, but **including our PR #7 dogfood App, Excalidraw**, three real Apps declare external domains the static CSP can't honor — they fail at runtime trying to fetch declared CDN scripts / tiles / fonts / soundfonts under our `connect-src 'self'`. Excalidraw's `create_view` is the canonical case: its server declares `resourceDomains: ['https://esm.sh']` + `connectDomains: ['https://esm.sh']` (see `excalidraw/excalidraw-mcp/src/server.ts`), and the dogfood console shows a wall of blocked esm.sh font / script / stylesheet loads. The spec's draft `apps.mdx` line 283 makes this a **host MUST**: "Host MUST construct CSP headers based on declared domains." We're not currently violating "MUST NOT allow undeclared domains" (we have no externals in our CSP at all), but we're failing the contract Apps rely on. Implementation: a CloudFront Function on viewer-response reading `?csp=` matching the upstream `examples/basic-host/serve.ts` `buildCspHeader` — ~50–100 LoC across `infrastructure/assets/mcp-sandbox/csp-function.js`, `mcp-sandbox-stack.ts`, frontend `proxy-url.ts`, plus tests. Cache stays simple (CFN runs on viewer-response including cache hits; one cached `proxy.html` body, dynamic header per request).

## Apps that need it

Empirical scan of `modelcontextprotocol/ext-apps/examples/*-server/server.ts` and the Excalidraw MCP server for `_meta.ui.csp` declarations. Four servers declare external domains:

### Excalidraw `create_view` (our dogfood)

```typescript
// excalidraw/excalidraw-mcp/src/server.ts
const cspMeta = {
  ui: {
    csp: {
      resourceDomains: ['https://esm.sh'],
      connectDomains: ['https://esm.sh'],
    },
  },
};
```

The view's HTML pulls React 19, ReactDOM, Excalidraw 0.18, and the font/CSS bundle from `esm.sh`. On broad static CSP every one of those loads is blocked (`script-src` / `style-src` / `font-src` allow only `'self' blob: data:` — no `esm.sh`). The dogfood demo is visibly broken until this lands.

### map-server (CesiumJS globe + OSM tiles)

```typescript
const cspMeta = {
  ui: {
    csp: {
      connectDomains: [
        "https://*.openstreetmap.org",   // OSM tiles + Nominatim geocoding
        "https://cesium.com",
        "https://*.cesium.com",
      ],
      resourceDomains: [
        "https://*.openstreetmap.org",   // OSM map tiles
        "https://cesium.com",
        "https://*.cesium.com",
      ],
    },
  },
};
```

Hard fail on broad static — Cesium needs the tile servers + CDN both for `connect` (XHR for tile bytes / geocoding) and `resource` (script-src for ion-loaded JS modules). Our `connect-src 'self'` blocks every tile request the moment the globe initialises.

### pdf-server (PDF.js standard fonts)

```typescript
csp: {
  // pdf.js loads the Standard-14 fonts TWO ways:
  //   - fetch()s the .ttf bytes → connect-src
  //   - creates FontFace('name', 'url(...)') → font-src
  // resourceDomains maps to font-src; we need both.
  connectDomains: [STANDARD_FONT_ORIGIN],
  resourceDomains: [STANDARD_FONT_ORIGIN],
},
```

`STANDARD_FONT_ORIGIN` resolves to the pdf.js CDN host. PDF body renders but every glyph that requires a Standard-14 font (Helvetica, Times, Courier, Symbol, ZapfDingbats) falls back to a substitute or renders as a box — a visible quality regression, not a hard fail.

### sheet-music-server (audio soundfonts)

```typescript
csp: {
  // Allow loading soundfonts for audio playback
  connectDomains: ["https://paulrosen.github.io"],
},
```

Visual sheet-music rendering works on broad static (abcjs is bundled). Only the "play audio" button silently fails — soundfont fetches hit `connect-src 'self'` block.

## Apps that don't need it

22 of 25 reference servers declare no `_meta.ui.csp` at all. These work today on our broad static CSP because they:

- Bundle everything (no external CDN fetches).
- Use only same-origin postMessage to the host (no external network).
- Use only `permissions` (mic/camera/clipboard) without external resource needs — covered by our `_meta.ui.permissions` plumbing, not CSP.

Concrete list: `basic-server-*` (preact/react/solid/svelte/vanillajs/vue), budget-allocator-server, scenario-modeler-server, cohort-heatmap-server, customer-segmentation-server, integration-server, transcript-server, debug-server, qr-server, say-server, shadertoy-server, system-monitor-server, threejs-server, video-resource-server, wiki-explorer-server. **All five of the "rich UI" candidates the scoping doc considered for PR #7 dogfood** (budget-allocator, scenario-modeler, threejs, shadertoy, transcript) are in this set — none of them are blocked.

Note: shadertoy / threejs being in this set is non-obvious — they're WebGL-heavy and you'd expect external asset CDNs, but in the reference repo they ship fully bundled.

## Cost vs. benefit

### Security gain — small, bordering on theatre

The threat model: "untrusted App HTML escapes its CSP and exfiltrates / phishes from the user." Our current static CSP has:

- `connect-src 'self'` — App cannot make any external network request from inside the iframe.
- `frame-src 'none'` — App cannot frame anything else.
- `base-uri 'none'`, `form-action 'none'`, `object-src 'none'` — no base / form / plugin injection.

The remaining attack surface is `'unsafe-inline' 'unsafe-eval' blob: data:` on scripts/styles. But:

1. The inner App iframe is **already cross-origin sandboxed** to the SPA (null origin under `sandbox` attribute). Even if an attacker fully owns the App's JS execution, they can't reach SPA cookies, localStorage, or DOM.
2. The outer `proxy.html` ships **zero inline content** — every byte that runs is `proxy.js` loaded from same-origin (the dedicated mcp-sandbox CloudFront). `'unsafe-inline'`/`'unsafe-eval'` on the outer document can't be exploited unless an attacker can already inject into a static CloudFront asset, which is a much bigger compromise.
3. Going dynamic would *narrow* `connect-src` and `script-src` to per-App declared domains. But for the 22/25 Apps without declared CSP, we'd use the spec's restrictive default (`connect-src 'none'`, `script-src 'self' 'unsafe-inline'` — *no* `'unsafe-eval' blob:`), which would **break** many of the bundled-but-eval-needing Apps we currently render fine. The reference implementation acknowledges this by baking `'unsafe-eval' blob: data:` into its default too.

So dynamic CSP buys us: a tighter `connect-src` for the 3 Apps that actually declare it. That's a marginal defense-in-depth gain stacked behind the existing cross-origin sandbox boundary.

### Spec-compliance gain — real but not violated today

Draft `apps.mdx` line 283: **"Host MUST construct CSP headers based on declared domains."**
Line 295: "No Loosening: Host MAY further restrict but MUST NOT allow undeclared domains."

We don't violate "MUST NOT allow undeclared domains" — we have no external domains in our CSP at all. We *do* violate "MUST construct CSP headers based on declared domains" in the sense that we ignore declared `connectDomains`/`resourceDomains`. The user-visible consequence is that map-server / pdf-server / sheet-music-server can't fully function on us — they DECLARED what they need, we DIDN'T honor the declaration, the App fails. That's not "leaky security," it's "host doesn't implement the contract the App relied on."

If someone is grading us on spec compliance (an external review, an audit, an MCP showcase), this gap is visible. If we're shipping internally, no one notices until we onboard a CSP-declaring App.

### Implementation options compared

| Option | Code | Deploy time | Runtime cost | Cache impact | On-call |
|---|---|---|---|---|---|
| **A. CloudFront Function (viewer-request → -response)** | ~30 LoC JS, no async, sanitize `?csp=`, emit header | Standard CFN deploy (~5 min) | $0.10/M invocations — pennies | proxy.html cache key adds `?csp`; hit rate drops to ~0 but origin is S3 (fast). proxy.js unaffected | Low — sync function, no cold start, no env vars |
| **B. Lambda@Edge (viewer-response)** | ~50 LoC Node, full SDK, easier to test | Slower deploy (~10 min replication) | $0.60/M + duration; <$1/month at our traffic | same as A | Medium — Lambda@Edge logs land in *viewer* region CloudWatch, harder to follow; rollback is slower |
| **C. Replace CloudFront+S3 with API Gateway + Lambda** | ~150 LoC + CDK rewrite | New stack | Higher | Lose CloudFront edge cache for proxy.js too | High — bigger surface |
| **D. Origin Lambda behind CloudFront** | Lambda + CFN integration | Standard | Higher than A/B | proxy.js still cacheable; proxy.html per-request | Medium |

Plus, for any option, frontend side: `mcp-app-frame.component.ts` already has `csp` from the `ui_resource` SSE event — it would build `${proxyOrigin}/proxy.html?csp=${encodeURIComponent(JSON.stringify(csp))}` before assigning `iframe.src`. ~10 LoC change, no new SSE event.

**Recommended option if/when we ship: A (CloudFront Function).** It fits the constraint set (sync, no I/O, sanitize + concat into a header), is cheaper and lower-latency than Lambda@Edge, and has the simpler operational story. The sanitizer from `serve.ts` (`/[;\r\n'" ]/.test(d)` reject) is straightforwardly portable to the CFN JS runtime.

The `ResponseHeadersPolicy` in `infrastructure/lib/mcp-sandbox-stack.ts` would need to drop its static `Content-Security-Policy` (the dynamic header would conflict with the policy's "override: true" semantics). Other security headers (HSTS, Referrer-Policy, X-Content-Type-Options) stay in the policy. `frame-ancestors` becomes part of the dynamic CSP since it's the security-critical bit — though it could also stay in a separate static `Content-Security-Policy` header alongside the dynamic one (CSPs combine via intersection).

### Cache implications

Today: `CacheQueryStringBehavior.none()` — every request to `proxy.html` returns the same cached body. Switch to `CacheQueryStringBehavior.allowList(['csp'])` and each unique `?csp=` value becomes a separate cache entry. With ~hundreds of distinct Apps in any deployed env, hit rate on `proxy.html` drops from ~100% to ~0%. proxy.html is ~2 KB, S3 origin response is sub-10ms — the cost is invisible at our traffic. `proxy.js` cache is untouched (no query param on its fetch).

One real concern: cache *explosion* if Apps generate per-call unique `?csp=` query strings (e.g. dynamic per-conversation CSP). The 25 reference Apps all use static `_meta.ui.csp` at resource-declaration time, so in practice the cardinality is bounded by the number of distinct UI resources, not the number of conversations.

## Trigger — what would change the recommendation

**Ship if any of these happen:**

1. We onboard map-server, pdf-server, or sheet-music-server (or any App declaring non-`'self'` `connectDomains` / `resourceDomains` / `frameDomains` / `baseUriDomains`). The CSP work goes in *that* PR — same author, fresh context, no need to reload prior state. **Most likely trigger: CesiumJS map-server when we want a "wow" demo.**
2. The spec MUST tightens further (e.g. "Host MUST reject UI resources that declare CSP the host doesn't honor"). Skim the draft on each kaizen-research pass — currently line 283 is the relevant MUST; nothing has been added that makes it a *rejection* requirement yet.
3. An external review / showcase / partner asks for SEP-1865 compliance attestation. The "declared domains not honored" gap is visible to anyone who reads the spec.
4. We onboard an App that needs nested iframes (`frameDomains`) — our static `frame-src 'none'` blocks all nested framing absolutely. Reference Apps that fit this profile: none today, but anything embedding YouTube / a Tableau viz / a third-party widget would need it.

**Don't ship for:**

- "Defense-in-depth feels nice." The cross-origin sandbox is the real boundary. CSP tightening is icing.
- "The reference does it, so should we." The reference is a demo host; we're a product. Match capability when we have a user-facing reason.
- "It's in the scoping doc as a risk." The original scoping doc (`docs/kaizen/scoping/mcp-apps-host-renderer.md`) called out the CSP/`frame-ancestors` interplay as a 0.5–1d debug; we paid that debt. The dynamic-per-resource piece was always a follow-up.

## Files that would change if we ship

For reference (not implementation):

- `infrastructure/lib/mcp-sandbox-stack.ts` — new CloudFront Function resource, drop static CSP from `ResponseHeadersPolicy`, update `CachePolicy` to include `?csp` in cache key on the `proxy.html` path behavior.
- `infrastructure/lib/mcp-sandbox-function.js` (new) — the CFN handler: read `?csp=`, parse JSON, sanitize domains, build CSP string (mirror `buildCspHeader` from `examples/basic-host/serve.ts`), set response header.
- `infrastructure/test/mcp-sandbox-stack.test.ts` — unit tests for sanitization (the `/[;\r\n'" ]/` reject rule is security-critical — every CSP-injection attack hides in domain entries with embedded `'`/`;`/space).
- `frontend/ai.client/src/app/.../mcp-app-frame.component.ts` — build `?csp=` query before setting `iframe.src`; the bridge already receives `csp` on the `ui_resource` event.
- `frontend/ai.client/src/app/.../mcp-app-frame.component.spec.ts` — unit test the query-string encoding.
- No backend changes (the `ui_resource` SSE event already carries `csp`).

Total: 1 new file (CFN handler), edits to 4 files, ~80–100 LoC + tests. 1–2 days, mostly testing the cache invalidation + redeploy behavior end-to-end.
