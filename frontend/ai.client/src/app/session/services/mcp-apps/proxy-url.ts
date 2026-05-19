import type { McpUiCsp } from '../../../shared/utils/stream-parser';

/**
 * Build the proxy.html URL the `<mcp-app-frame>` iframe `src` is set to.
 *
 * The sandbox-proxy origin runs a CloudFront Function on viewer-response
 * (`infrastructure/assets/mcp-sandbox/csp-function.js`) that composes the
 * `Content-Security-Policy` header from a `?csp=` query parameter. Apps
 * that declare `_meta.ui.csp` get a per-resource CSP that honors their
 * declared `connectDomains` / `resourceDomains` / `frameDomains` /
 * `baseUriDomains`; Apps that omit it (or declare an empty shape) get the
 * default CSP from the function with no query string at all — matching
 * the upstream `examples/basic-host/serve.ts` reference.
 *
 * We only attach `?csp=` when the resource actually declares at least one
 * non-empty domain array. An empty `{}` or `{ connectDomains: [] }` would
 * produce identical CSP output from the function either way, and omitting
 * the param keeps CloudFront cache keys uniform across the no-declaration
 * majority of Apps.
 */
export function buildProxyUrl(
  sandboxOrigin: string,
  csp: McpUiCsp | null | undefined,
): string {
  const base = `${sandboxOrigin.replace(/\/$/, '')}/proxy.html`;
  if (!hasDeclaredDomains(csp)) return base;
  return `${base}?csp=${encodeURIComponent(JSON.stringify(csp))}`;
}

function hasDeclaredDomains(csp: McpUiCsp | null | undefined): csp is McpUiCsp {
  if (!csp || typeof csp !== 'object') return false;
  return (
    isNonEmptyArray(csp.connectDomains) ||
    isNonEmptyArray(csp.resourceDomains) ||
    isNonEmptyArray(csp.frameDomains) ||
    isNonEmptyArray(csp.baseUriDomains)
  );
}

function isNonEmptyArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0;
}
