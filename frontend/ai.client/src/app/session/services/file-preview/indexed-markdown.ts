import { Marked } from 'marked';

/**
 * Render the markdown a knowledge-base document was indexed from.
 *
 * The text is someone else's: a crawled page, or a document the agent's owner
 * uploaded. So this renders the markdown and nothing that rides along in it:
 *
 * - raw HTML (block or inline) is shown as escaped text, never parsed;
 * - images render as their alt text, so opening a document never fetches a
 *   third-party URL from the viewer's browser;
 * - links keep only http(s) and mailto addresses, open in a new tab, and a
 *   relative link (which would resolve against this app, not the page it was
 *   crawled from) renders as its text.
 *
 * Its own `Marked` instance, not the app's global one, so these rules never leak
 * into chat rendering and the chat renderer's rules (the download-link rewrite)
 * never apply here. The caller binds the result through `[innerHTML]`, so
 * Angular's sanitizer still runs over it as a second layer.
 */
const indexedMarkdown = new Marked({
  async: false,
  gfm: true,
  renderer: {
    html({ text }) {
      return escapeHtml(text);
    },
    image({ text }) {
      return escapeHtml(text);
    },
    link({ href, title, tokens }) {
      const label = this.parser.parseInline(tokens);
      const safe = safeHref(href);
      if (!safe) return label;
      const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
      return `<a href="${escapeHtml(safe)}"${titleAttr} target="_blank" rel="noopener noreferrer">${label}</a>`;
    },
  },
});

export function renderIndexedMarkdown(markdown: string): string {
  return indexedMarkdown.parse(markdown) as string;
}

const SAFE_LINK_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);

/** An absolute http(s) or mailto address, or null. */
export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null;
  let parsed: URL;
  try {
    parsed = new URL(href);
  } catch {
    return null;
  }
  return SAFE_LINK_PROTOCOLS.has(parsed.protocol) ? parsed.href : null;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
