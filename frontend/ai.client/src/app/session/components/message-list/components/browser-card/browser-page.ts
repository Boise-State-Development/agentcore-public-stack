import type { ToolCallDisplay } from '../tool-rail/tool-rail.model';

/** The tool whose calls produce a page card. */
export const BROWSE_WEB_TOOL = 'browse_web';

/** What a run of `browse_web` calls left the browser looking at. */
export interface BrowserPageSummary {
  /** The last page the browser was on. Always http(s). */
  url: string;
  host: string;
  /** `document.title` from the last navigate, when it still describes `url`. */
  title: string | null;
  /** The most recent screenshot the agent took, as a data URL. */
  screenshot: string | null;
  /** A call is still running. */
  browsing: boolean;
  /** The agent closed the browser session at the end of the run. */
  closed: boolean;
}

/** `example.com` from a URL, dropping a leading `www.`; null when unparseable. */
export function hostLabel(url: string): string | null {
  try {
    const host = new URL(url).hostname;
    return host ? host.replace(/^www\./, '') : null;
  } catch {
    return null;
  }
}

/**
 * Only http(s) URLs become links or labels. The URL comes from a remote page
 * (via `location.href`), so anything else — `javascript:`, `data:` — is
 * dropped rather than rendered.
 */
function safeHttpUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = new URL(value.trim());
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

const IMAGE_FORMATS = new Set(['png', 'jpeg', 'jpg', 'gif', 'webp']);

// Shapes from `browse_tool.py`: navigate returns "Navigated to <url>\nTitle:
// <title>", click returns "Current URL: <url>".
const NAVIGATED = /Navigated to (\S+)\s*\nTitle: ([^\n]*)/;
const CURRENT_URL = /Current URL: (\S+)/;

/**
 * Summarise a rail's `browse_web` calls into the page the card shows, or null
 * when none of them reached a page.
 *
 * Reads the tool results rather than adding a new SSE event: the navigate
 * result already carries the URL and title, and a screenshot result already
 * carries the image, so a card costs nothing on the turn path and survives a
 * reload from persisted messages alone.
 */
export function browserPageFromCalls(
  calls: readonly ToolCallDisplay[],
): BrowserPageSummary | null {
  let url: string | null = null;
  let title: string | null = null;
  let screenshot: string | null = null;
  let browsing = false;
  let closed = false;

  for (const call of calls) {
    if (call.toolName !== BROWSE_WEB_TOOL) continue;
    const action = call.input['action'];

    if (call.status === 'pending') {
      browsing = true;
      if (action === 'navigate') {
        const target = safeHttpUrl(call.input['url']);
        if (target && target !== url) {
          url = target;
          title = null;
        }
      }
      continue;
    }

    if (action === 'close' && call.status === 'complete') {
      closed = true;
      continue;
    }
    closed = false;

    for (const item of call.result?.content ?? []) {
      if (item.text) {
        const navigated = NAVIGATED.exec(item.text);
        if (navigated) {
          const next = safeHttpUrl(navigated[1]);
          if (next) {
            url = next;
            title = navigated[2].trim() || null;
          }
          continue;
        }
        const current = CURRENT_URL.exec(item.text);
        const next = current ? safeHttpUrl(current[1]) : null;
        if (next && next !== url) {
          // A click that changed pages: the old title no longer applies.
          url = next;
          title = null;
        }
      }
      const format = String(item.image?.format ?? '').toLowerCase();
      if (item.image?.data && IMAGE_FORMATS.has(format)) {
        const mime = format === 'jpg' ? 'jpeg' : format;
        screenshot = `data:image/${mime};base64,${item.image.data}`;
      }
    }
  }

  if (!url) return null;
  return {
    url,
    host: hostLabel(url) ?? url,
    title,
    screenshot,
    browsing,
    closed,
  };
}
