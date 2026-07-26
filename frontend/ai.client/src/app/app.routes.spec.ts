import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Component } from '@angular/core';
import { provideRouter, Router, Routes } from '@angular/router';
import { provideLocationMocks } from '@angular/common/testing';
import { routes } from './app.routes';

/**
 * Assistant-deprecation redirects (Designer Phase 5, #746).
 *
 * `/assistants*` no longer renders anything — it redirects onto the Agent surface. These
 * paths are in people's bookmarks, in the "edit" link of every old chat session, and in
 * links colleagues shared with each other, so the redirect is the compatibility promise:
 * the ids are the same record on both sides (the compat mapping renders a legacy
 * Assistant *as* an Agent — nothing was migrated), so the redirect lands on the same
 * thing the old URL opened.
 *
 * Asserted against the real route table rather than a hand-built one: the bug this guards
 * against is someone deleting the redirect entries, and a fixture table would not notice.
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

  it('sends the assistants list to the agents hub', async () => {
    await router.navigateByUrl('/assistants');
    expect(router.url).toBe('/agents');
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
