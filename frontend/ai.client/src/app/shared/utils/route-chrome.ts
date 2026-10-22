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
 * Route `data` flag the admin console sets on its parent route.
 *
 * The shell keeps the sidenav but swaps its body for the admin
 * navigation, and drops the centred `max-w-7xl` content box so an admin
 * page gets the whole width. The console's tables and dashboards were
 * paying twice for the chat chrome — once for a conversation list they
 * cannot use from `/admin`, and again for a reading-width cap meant for
 * prose.
 */
export const ADMIN_CHROME = 'admin';

/**
 * The chrome the *deepest route that declares one* asks for, or `null`.
 *
 * The walk to the leaf matters two ways. `data` is declared on the route
 * that owns the page and the shell reads it from the router's root
 * snapshot, so stopping at the root would never see a leaf's flag. And
 * the admin console declares its flag on the *parent* `/admin` route,
 * whose children each declare none — with the default
 * `paramsInheritanceStrategy` ('emptyOnly') Angular does not copy `data`
 * onto a child that has its own path and component, so a leaf-only read
 * would see nothing on every admin page.
 *
 * "Deepest declared wins" satisfies both: a leaf that declares its own
 * chrome overrides an ancestor's, and a leaf that declares none inherits
 * it. A leaf can opt back out by declaring anything else (`'full'`).
 *
 * Split out of the shell component so the traversal is testable without
 * mounting the whole app (see the note in `app.spec.ts` about why that
 * spec avoids static Angular imports).
 */
export function resolveRouteChrome(
  root: ActivatedRouteSnapshot | null | undefined,
): string | null {
  let route = root;
  let chrome: string | null = null;
  while (route) {
    const declared = route.data?.['chrome'];
    if (typeof declared === 'string') chrome = declared;
    route = route.firstChild;
  }
  return chrome;
}

/** Whether the active route asks for the stripped shell. */
export function isMinimalChromeRoute(
  root: ActivatedRouteSnapshot | null | undefined,
): boolean {
  return resolveRouteChrome(root) === MINIMAL_CHROME;
}

/** Whether the active route is inside the admin console. */
export function isAdminChromeRoute(
  root: ActivatedRouteSnapshot | null | undefined,
): boolean {
  return resolveRouteChrome(root) === ADMIN_CHROME;
}
