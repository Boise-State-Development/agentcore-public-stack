import type { ActivatedRouteSnapshot } from '@angular/router';

/**
 * Route `data` flag a page sets to ask the app shell for a stripped
 * layout: no sidenav, no centred/padded content box.
 *
 * Used by a page that is the whole point of the visit rather than one
 * view inside the app — a shared artifact opened from a link, say.
 */
export const MINIMAL_CHROME = 'minimal';

/**
 * Whether the *deepest* activated route asks for minimal chrome.
 *
 * The walk to the leaf matters: `data` is declared on the route that
 * owns the page, and the shell reads it from the router's root
 * snapshot, so stopping at the root would never see it. Angular does
 * inherit `data` down to children, but not *up* — a parent cannot tell
 * you what its child asked for.
 *
 * Split out of the shell component so the traversal is testable without
 * mounting the whole app (see the note in `app.spec.ts` about why that
 * spec avoids static Angular imports).
 */
export function isMinimalChromeRoute(
  root: ActivatedRouteSnapshot | null | undefined,
): boolean {
  let route = root;
  if (!route) return false;
  while (route.firstChild) route = route.firstChild;
  return route.data?.['chrome'] === MINIMAL_CHROME;
}
