import { describe, it, expect, beforeEach } from 'vitest';
import { Component, input } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Route, Router, provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { PROJECT_ROUTES } from './projects.routes';
import { unsavedChangesGuard } from './settings/unsaved-changes.guard';

/** Stands in for every page, showing the inputs the router bound to it. */
@Component({ selector: 'app-page-stub', template: '{{ id() }}:{{ kind() ?? "" }}' })
class PageStub {
  readonly id = input<string>();
  readonly kind = input<string>();
}

/**
 * Where each `/projects/:id/...` URL lands. The pages themselves are stubbed (each
 * has its own spec); this is about the table: real pages for every rail row but
 * Model, and the tab URLs that predate them (the bell's, old bookmarks) still
 * landing somewhere sensible.
 */
describe('PROJECT_ROUTES', () => {
  const stubbed = (routes: Route[]): Route[] =>
    routes.map(r => (r.loadComponent ? { ...r, loadComponent: undefined, component: PageStub, canDeactivate: [] } : r));

  let harness: RouterTestingHarness | null;

  beforeEach(() => {
    harness = null;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([{ path: 'projects/:id', children: stubbed(PROJECT_ROUTES) }], withComponentInputBinding())],
    });
  });

  async function land(url: string): Promise<{ url: string; text: string }> {
    harness ??= await RouterTestingHarness.create();
    await harness.navigateByUrl(url);
    harness.detectChanges();
    return { url: TestBed.inject(Router).url, text: (harness.routeNativeElement as HTMLElement | null)?.textContent ?? '' };
  }

  it('gives each settings row a page of its own, with the project id bound', async () => {
    for (const page of ['overview', 'memory', 'instructions', 'files', 'tools', 'skills', 'schedules', 'members', 'activity', 'history']) {
      const landed = await land(`/projects/prj_1/${page}`);
      expect(landed.url).toBe(`/projects/prj_1/${page}`);
      expect(landed.text).toContain('prj_1:');
    }
  });

  it('tells the bindings page which kind it is from the route', async () => {
    expect((await land('/projects/prj_1/tools')).text).toBe('prj_1:tools');
    expect((await land('/projects/prj_1/skills')).text).toBe('prj_1:skills');
  });

  it('opens the overview from the bare project URL', async () => {
    expect((await land('/projects/prj_1')).url).toBe('/projects/prj_1/overview');
  });

  it('sends the old Settings tab to Instructions, and anything else it does not know to the overview', async () => {
    expect((await land('/projects/prj_1/settings')).url).toBe('/projects/prj_1/instructions');
    for (const tab of ['tasks', 'model', 'nonsense']) {
      expect((await land(`/projects/prj_1/${tab}`)).url).toBe('/projects/prj_1/overview');
    }
  });

  it('keeps query params a link carries', async () => {
    expect((await land('/projects/prj_1/history?version=3')).url).toBe('/projects/prj_1/history?version=3');
    expect((await land('/projects/prj_1/memory?view=review')).url).toBe('/projects/prj_1/memory?view=review');
  });

  it('guards the pages that hold a draft until Save', () => {
    const guarded = PROJECT_ROUTES.filter(r => r.canDeactivate?.includes(unsavedChangesGuard)).map(r => r.path);
    expect(guarded).toEqual(['instructions', 'tools', 'skills']);
  });
});
