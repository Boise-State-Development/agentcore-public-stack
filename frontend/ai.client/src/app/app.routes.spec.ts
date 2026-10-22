import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Component } from '@angular/core';
import { provideRouter, Router, Routes } from '@angular/router';
import { provideLocationMocks } from '@angular/common/testing';
import { routes } from './app.routes';
import { MINIMAL_CHROME } from './shared/utils/route-chrome';

/**
 * Assistant deprecation (Designer Phase 5, #746).
 *
 * The two **deep** `/assistants/*` paths redirect onto the Agent surface. They are in
 * people's bookmarks, in the "edit" link of every old chat session, and in links
 * colleagues shared with each other, so the redirect is the compatibility promise: the
 * ids are the same record on both sides (the compat mapping renders a legacy Assistant
 * *as* an Agent — nothing was migrated), so the redirect lands on the same thing the old
 * URL opened.
 *
 * The bare `/assistants` **list** URL does not redirect: it renders the migration
 * explainer, because that URL is browsed to rather than acted on, and a silent bounce
 * leaves "where did my assistants go" unanswered.
 *
 * Asserted against the real route table rather than a hand-built one: the bug this guards
 * against is someone deleting these entries, and a fixture table would not notice.
 */
describe('app routes — assistant deprecation redirects', () => {
  @Component({ template: '' })
  class BlankComponent {}

  /**
   * The real table, with every lazy `loadComponent` swapped for a blank component.
   *
   * Navigation must actually resolve for the router to report a final URL, and resolving
   * the real pages would drag in their whole dependency graphs. The **paths** and
   * `redirectTo` entries — the only thing under test — are preserved exactly.
   */
  function stubbedRoutes(source: Routes): Routes {
    return source.map((route) => {
      const { loadComponent, loadChildren, children, canActivate, ...rest } = route;
      const stubbed: Routes[number] = { ...rest };
      if (children) stubbed.children = stubbedRoutes(children);
      if ((loadComponent || loadChildren) && !rest.redirectTo) stubbed.component = BlankComponent;
      return stubbed;
    });
  }

  let router: Router;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter(stubbedRoutes(routes)), provideLocationMocks()],
    });
    router = TestBed.inject(Router);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('keeps the assistants list URL on itself so the explainer can render', async () => {
    // Not a redirect. Someone who bookmarked their Assistants list gets told what
    // happened to it; bouncing them silently onto /agents is the bug this guards against.
    await router.navigateByUrl('/assistants');
    expect(router.url).toBe('/assistants');
  });

  it('sends the new-assistant form to the Designer', async () => {
    await router.navigateByUrl('/assistants/new');
    expect(router.url).toBe('/agents/new');
  });

  it('sends a bookmarked assistant editor to the same record in the Designer', async () => {
    // The id must survive the redirect — that is the whole compatibility promise.
    await router.navigateByUrl('/assistants/ast-001/edit');
    expect(router.url).toBe('/agents/ast-001/edit');
  });

  it('does not swallow the agents routes it redirects onto', async () => {
    await router.navigateByUrl('/agents/discover');
    expect(router.url).toBe('/agents/discover');

    await router.navigateByUrl('/agents/ast-001');
    expect(router.url).toBe('/agents/ast-001');
  });
});

describe('app routes — shell chrome', () => {
  it('asks for a minimal shell on the shared-artifact route', () => {
    const route = routes.find(r => r.path === 'shared-artifact/:shareId');
    expect(route).toBeDefined();
    // A recipient followed a link to view one thing; the shell reads
    // this to drop the sidenav and the centred content box.
    expect(route!.data?.['chrome']).toBe(MINIMAL_CHROME);
  });

  it('asks for a minimal shell on the artifact viewer route', () => {
    const route = routes.find(r => r.path === 'artifacts/:artifactId');
    expect(route).toBeDefined();
    // Not styling. The full shell's content box has no definite height,
    // so a viewer laid out to fill it collapses to a ~150px iframe. The
    // minimal branch is the only one that hands a route real height.
    expect(route!.data?.['chrome']).toBe(MINIMAL_CHROME);
  });

  it('leaves every other route on the full shell', () => {
    // Opt-in by design: the flag strips app navigation, so it should
    // never spread by accident. Both entries here are artifact viewers,
    // which is the shape that needs it — one thing, filling the shell,
    // carrying its own way back.
    const minimal = routes
      .filter(r => r.data?.['chrome'] === MINIMAL_CHROME)
      .map(r => r.path);
    expect(minimal).toEqual(['shared-artifact/:shareId', 'artifacts/:artifactId']);
  });
});

/**
 * Connectors moved out of Settings and into Customize (step 2 of
 * `docs/specs/customize-surface.md`): connecting an account and enabling the tools
 * that need it are one user intent, and keeping them on separate pages only got
 * worse once Tools moved to Customize.
 *
 * `/settings/connectors` stays as a redirect rather than a deletion. It is in
 * bookmarks, and the schedules page linked users straight to it for a long time —
 * deleting it would turn every one of those into the catch-all 404.
 *
 * ⚠️ Order-sensitive: `settings/connectors` must be declared BEFORE the `settings`
 * route, whose `loadChildren` would otherwise swallow the path and land the user on
 * the settings shell with no matching child.
 */
describe('app routes — connectors moved into Customize', () => {
  @Component({ template: '' })
  class BlankComponent {}

  function stubbedRoutes(source: Routes): Routes {
    return source.map((route) => {
      const { loadComponent, loadChildren, children, canActivate, ...rest } = route;
      const stubbed: Routes[number] = { ...rest };
      if (children) stubbed.children = stubbedRoutes(children);
      if ((loadComponent || loadChildren) && !rest.redirectTo) stubbed.component = BlankComponent;
      return stubbed;
    });
  }

  let router: Router;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter(stubbedRoutes(routes)), provideLocationMocks()],
    });
    router = TestBed.inject(Router);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('sends the old settings deep link to the Customize tab', async () => {
    await router.navigateByUrl('/settings/connectors');
    expect(router.url).toBe('/customize/connectors');
  });

  it('declares the redirect before the settings shell that would swallow it', () => {
    const paths = routes.map(r => r.path);
    expect(paths.indexOf('settings/connectors')).toBeGreaterThan(-1);
    expect(paths.indexOf('settings/connectors')).toBeLessThan(paths.indexOf('settings'));
  });

  it('serves the connectors tab in its own right', async () => {
    await router.navigateByUrl('/customize/connectors');
    expect(router.url).toBe('/customize/connectors');
  });

  it('keeps the other Customize tabs reachable', async () => {
    await router.navigateByUrl('/customize');
    expect(router.url).toBe('/customize/tools');
    await router.navigateByUrl('/customize/skills');
    expect(router.url).toBe('/customize/skills');
  });

  it('leaves the rest of Settings alone', async () => {
    await router.navigateByUrl('/settings/profile');
    expect(router.url).toBe('/settings/profile');
  });

  it('no longer offers connectors as a Settings child route', async () => {
    const { settingsRoutes } = await import('./settings/settings.routes');
    expect(settingsRoutes.map(r => r.path)).not.toContain('connectors');
  });
});
