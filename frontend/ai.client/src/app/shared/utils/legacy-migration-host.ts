import { DOCUMENT } from '@angular/common';
import { InjectionToken, inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

/**
 * ⚠️ TEMPORARY — the Assistants signpost is scoped to the production apex.
 *
 * `/assistants` and its sidenav entry exist for one audience: people arriving at the
 * production site who built assistants in the **previous** version of boisestate.ai (now
 * parked at `legacy.boisestate.ai`) and want to know where those went. Nobody else has that
 * question, so the deployed environments that are not production do not get the page —
 * neither `dev.boisestate.ai` nor `beta.boisestate.ai`.
 *
 * `localhost` is on the list for the opposite reason: it is not an audience, it is the bench.
 * Copy is the entire substance of this page, and copy that cannot be read in place gets
 * reviewed by imagination — so anyone working on it can see it without editing the gate,
 * which is also how a "just for a second" local edit stops ending up in a commit.
 *
 * This is a client-side courtesy gate, not a security boundary: the page is copy and two
 * outbound links, and anyone who flips it back on in devtools has learned nothing they could
 * not read here. It is scoping, not access control.
 *
 * The whole thing — this file, the sidenav entry, the route, `agents/migration/` — comes out
 * when the legacy site is retired.
 *
 * **To preview on another host**, override `LEGACY_MIGRATION_HOST` in the app providers, or
 * add that host below; do not commit either.
 */
export const LEGACY_MIGRATION_HOSTS: readonly string[] = [
  'boisestate.ai',
  'www.boisestate.ai',
  // Local development only. Reachable from nowhere else, so it costs the gate nothing.
  'localhost',
];

/** Whether `hostname` is a host the Assistants signpost should appear on. */
export function isLegacyMigrationHost(hostname: string | null | undefined): boolean {
  if (!hostname) return false;
  return LEGACY_MIGRATION_HOSTS.includes(hostname.toLowerCase());
}

/**
 * Whether this page load is on a host that shows the Assistants signpost.
 *
 * A token rather than a bare `window.location` read at each call site, for two reasons: it
 * resolves once (the host cannot change without a full page load, so re-deriving it is
 * noise), and it gives specs a seam — override the token instead of trying to fake
 * `document.location`, which cannot be done without breaking the real `Document` the
 * TestBed needs for rendering.
 */
export const LEGACY_MIGRATION_HOST = new InjectionToken<boolean>('LEGACY_MIGRATION_HOST', {
  providedIn: 'root',
  factory: () => isLegacyMigrationHost(inject(DOCUMENT).location?.hostname),
});

/**
 * Route guard for `/assistants`. Off-host this behaves exactly as the route did before the
 * explainer existed: a silent redirect onto `/agents`. That is the right answer here — off
 * the apex there is no legacy site to explain, so the URL is only an old alias.
 */
export const legacyMigrationHostGuard: CanActivateFn = () => {
  const router = inject(Router);
  return inject(LEGACY_MIGRATION_HOST) || router.parseUrl('/agents');
};
