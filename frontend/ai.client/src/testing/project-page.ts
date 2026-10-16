import { EnvironmentProviders, Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Data, Route, provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { Project } from '../app/projects/models/project.model';

/** A project as `GET /projects/{id}` returns it to an editor. */
export const TEST_PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: 'Olive Owner',
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

/**
 * Route one project settings page at `projects/:id/<path>` the way the app does, so
 * `id`, route `data` and query params reach its inputs through
 * `withComponentInputBinding()`. A plain `/elsewhere` route is there to leave to.
 */
export function provideProjectPageRoute(
  path: string,
  component: Type<unknown>,
  options: { data?: Data; canDeactivate?: Route['canDeactivate'] } = {},
): EnvironmentProviders {
  return provideRouter(
    [
      { path: `projects/:id/${path}`, component, data: options.data, canDeactivate: options.canDeactivate },
      { path: 'elsewhere', children: [] },
    ],
    withComponentInputBinding(),
  );
}

/** Let the page's reads resolve and render. */
export async function settlePage(harness: RouterTestingHarness): Promise<void> {
  for (let i = 0; i < 3; i++) {
    await new Promise(resolve => setTimeout(resolve, 0));
    harness.detectChanges();
  }
  await harness.fixture.whenStable();
  TestBed.tick();
}

/**
 * Navigate to a project page and wait for it to settle. Angular allows one harness
 * per test, so a test that visits a second URL passes the first visit's back.
 */
export async function openProjectPage(
  url: string,
  existing?: RouterTestingHarness,
): Promise<{ harness: RouterTestingHarness; el: HTMLElement }> {
  const harness = existing ?? (await RouterTestingHarness.create());
  await harness.navigateByUrl(url);
  await settlePage(harness);
  return { harness, el: harness.routeNativeElement as HTMLElement };
}

/** The first button whose trimmed text is exactly `text`. */
export function buttonNamed(root: ParentNode, text: string): HTMLButtonElement | undefined {
  return Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(b => b.textContent?.trim() === text);
}
