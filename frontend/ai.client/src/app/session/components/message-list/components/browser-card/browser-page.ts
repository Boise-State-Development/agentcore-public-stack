import type { ToolResultContent } from '../../../../services/models/message.model';
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
// <title>", click returns "Current URL: <url>". Each is read only from its own
// action's result: navigate's result goes on to carry the page text, which
// could quote either shape.
const NAVIGATED = /Navigated to (\S+)\s*\nTitle: ([^\n]*)/;
const CURRENT_URL = /Current URL: (\S+)/;

type ResultImage = NonNullable<ToolResultContent['image']>;

// Screenshots run to megabytes of base64, and `displayBlocks` recomputes on
// every streamed delta. Keyed on the result's image object, which is stable
// across those recomputes, so each screenshot's data URL is built once.
const dataUrls = new WeakMap<ResultImage, string>();

function screenshotUrl(image: ResultImage | undefined): string | null {
  if (!image?.data) return null;
  const cached = dataUrls.get(image);
  if (cached) return cached;
  const format = String(image.format).toLowerCase();
  if (!IMAGE_FORMATS.has(format)) return null;
  const mime = format === 'jpg' ? 'jpeg' : format;
  const url = `data:image/${mime};base64,${image.data}`;
  dataUrls.set(image, url);
  return url;
}

/**
 * The card a rail's `browse_web` calls produce, or null when the rail shows
 * no card.
 *
 * A rail ends wherever the agent writes text, so one run of browsing is often
 * several rails: navigate, a line of narration, screenshot. `previous` is the
 * last card shown earlier in the same run, and carries the page forward, so a
 * screenshot-only rail still knows which page it captured. A rail shows a card
 * only when it changes what the user would see — it navigates, a click lands
 * on a new URL, or it takes a screenshot. Extracting text or links, typing,
 * evaluating and closing leave the page as the last card showed it.
 *
 * Reads the tool results rather than adding a new SSE event: the navigate
 * result already carries the URL and title, and a screenshot result already
 * carries the image, so a card costs nothing on the turn path and survives a
 * reload from persisted messages alone.
 */
export function browserPageFromCalls(
  calls: readonly ToolCallDisplay[],
  previous: BrowserPageSummary | null = null,
): BrowserPageSummary | null {
  let url = previous?.url ?? null;
  let title = previous?.title ?? null;
  let screenshot = previous?.screenshot ?? null;
  let browsing = false;
  let closed = false;
  let changed = false;

  // A screenshot belongs to the page it was taken on, so a new page drops it.
  const moveTo = (next: string, nextTitle: string | null) => {
    if (next !== url) screenshot = null;
    url = next;
    title = nextTitle;
    changed = true;
  };

  for (const call of calls) {
    if (call.toolName !== BROWSE_WEB_TOOL) continue;
    const action = call.input['action'];

    if (call.status === 'pending') {
      browsing = true;
      if (action === 'navigate') {
        const target = safeHttpUrl(call.input['url']);
        if (target) moveTo(target, target === url ? title : null);
      }
      continue;
    }

    if (action === 'close' && call.status === 'complete') {
      closed = true;
      continue;
    }
    closed = false;

    for (const item of call.result?.content ?? []) {
      if (action === 'navigate' && item.text) {
        const navigated = NAVIGATED.exec(item.text);
        const next = navigated ? safeHttpUrl(navigated[1]) : null;
        if (navigated && next) {
          // A navigate reloads the page even when the URL is unchanged.
          screenshot = null;
          moveTo(next, navigated[2].trim() || null);
        }
      } else if (action === 'click' && item.text) {
        const current = CURRENT_URL.exec(item.text);
        const next = current ? safeHttpUrl(current[1]) : null;
        // A click that changed pages: the old title no longer applies.
        if (next && next !== url) moveTo(next, null);
      } else if (action === 'screenshot') {
        const image = screenshotUrl(item.image);
        if (image) {
          screenshot = image;
          changed = true;
        }
      }
    }
  }

  if (!url || !changed) return null;
  return {
    url,
    host: hostLabel(url) ?? url,
    title,
    screenshot,
    browsing,
    closed,
  };
}
