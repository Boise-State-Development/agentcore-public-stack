import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Route } from '@angular/router';
import { FEATURES } from './features';
import { environment } from '../../environments/environment';
import { routes } from '../app.routes';

describe('FEATURES', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('is this build’s environment switches', () => {
    expect(TestBed.inject(FEATURES)).toBe(environment.features);
  });

  it('keeps local development opt-in: in-development features start off', () => {
    // environment.ts is what `ng serve` builds. A developer turns a feature on there
    // (and its backend *_ENABLED) by choice; see CLAUDE.md "Feature flags".
    expect(environment.features.projects).toBe(false);
  });
});

describe('/projects routes follow the Projects switch', () => {
  // `projects/:id` only redirects (Angular forbids a guard on a redirect); its target is guarded.
  const projectRoutes = routes.filter(r => r.path?.startsWith('projects') && !r.redirectTo);

  function matches(route: Route, projects: boolean): boolean {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [{ provide: FEATURES, useValue: { projects } }] });
    return (route.canMatch ?? []).every(guard =>
      TestBed.runInInjectionContext(() => (guard as () => boolean)()),
    );
  }

  it('covers every /projects route', () => {
    expect(projectRoutes.map(r => r.path)).toEqual(['projects/:id/:tab', 'projects']);
    expect(routes.find(r => r.path === 'projects/:id')?.redirectTo).toBe('projects/:id/overview');
    for (const route of projectRoutes) expect(route.canMatch?.length).toBeGreaterThan(0);
  });

  it('matches only when Projects are on', () => {
    for (const route of projectRoutes) {
      expect(matches(route, true)).toBe(true);
      expect(matches(route, false)).toBe(false);
    }
  });
});
