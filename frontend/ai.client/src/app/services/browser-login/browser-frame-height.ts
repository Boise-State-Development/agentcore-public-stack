/**
 * Height the docked browser panel spends on everything but the stream: its
 * header, the disclosure line and the frame container's `p-3`. Measured in
 * a 1440x900 window on 2026-10-17, where the frame got 778 of 900 px.
 *
 * Keep it in step with `BrowserPanelComponent`'s template. If it drifts high
 * the stream is handed over a little short and letterboxes; if it drifts low
 * the stream is a little tall and the panel scales it down a few percent.
 * Neither breaks the sign-in.
 */
export const BROWSER_PANEL_CHROME_PX = 122;

/**
 * The height, in CSS pixels, the docked panel would give the sign-in stream
 * in this window — sent on every chat request as `browser_frame_height`.
 *
 * Why every request: the backend sizes the remote browser once, before it
 * disables the agent's automation stream, because CDP is refused afterwards
 * (spec D3a). That happens inside whichever turn the agent decides to ask for
 * a sign-in, which the SPA cannot know in advance. The panel covers the
 * window's full height (`fixed inset-y-0`), expanded or docked, so the
 * window's height is the panel's.
 *
 * Client data only: the backend hands it to the takeover tool and never to
 * the model. Null when there is no window to measure (SSR, tests).
 */
export function browserFrameHeight(
  win: Pick<Window, 'innerHeight'> | null = typeof window === 'undefined' ? null : window,
): number | null {
  const height = win?.innerHeight;
  if (typeof height !== 'number' || !Number.isFinite(height)) return null;
  const frame = Math.floor(height - BROWSER_PANEL_CHROME_PX);
  return frame > 0 ? frame : null;
}
