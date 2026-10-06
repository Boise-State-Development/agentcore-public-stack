// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Component, computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { RouterOutlet, provideRouter } from '@angular/router';
import { TooltipDirective } from './components/tooltip/tooltip.directive';
import { SidenavService } from './services/sidenav/sidenav.service';
import { SessionService } from './auth/session.service';
import { SessionService as SessionListService } from './session/services/session/session.service';
import { DockedPaneService } from './session/services/docked-pane/docked-pane.service';
import { BrandingService } from '../branding/branding.service';

// The shell is loaded through a dynamic import, as in app.spec.ts, which
// transforms the App graph on demand; give it the same budget.
const IMPORT_TIMEOUT_MS = 30_000;

@Component({ selector: 'app-sidenav', template: '<input aria-label="Search conversations by title" />' })
class StubSidenav {}

@Component({ selector: 'app-error-toast', template: '' })
class StubErrorToast {}

@Component({ selector: 'app-toast', template: '' })
class StubToast {}

@Component({ selector: 'app-background-task-toasts', template: '' })
class StubBackgroundTaskToasts {}

/**
 * A collapsed desktop sidenav and a closing off-canvas drawer are only moved
 * off-screen, so without `inert` their controls (the conversation search box
 * among them) stay tabbable while invisible.
 */
describe('App shell: hidden sidenav panels are inert', () => {
  beforeEach(async () => {
    TestBed.resetTestingModule();
    const { App } = await import('./app');
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: SessionService, useValue: { recheck: () => undefined } },
        { provide: SessionListService, useValue: { refreshSessions: () => undefined } },
        { provide: DockedPaneService, useValue: { isOpen: signal(false), width: computed(() => 480) } },
        { provide: BrandingService, useValue: { pageTitle: 'Test' } },
      ],
    });
    TestBed.overrideComponent(App, {
      set: { imports: [RouterOutlet, TooltipDirective, StubSidenav, StubErrorToast, StubToast, StubBackgroundTaskToasts] },
    });
  }, IMPORT_TIMEOUT_MS);

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function render() {
    const { App } = await import('./app');
    const fixture = TestBed.createComponent(App);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  /** The panel element that directly wraps a mounted sidenav. */
  function panels(root: HTMLElement): HTMLElement[] {
    return Array.from(root.querySelectorAll('app-sidenav')).map(el => el.parentElement as HTMLElement);
  }

  it('marks the desktop panel inert only while collapsed', async () => {
    const fixture = await render();
    const sidenav = TestBed.inject(SidenavService);
    const [desktop] = panels(fixture.nativeElement);
    expect(desktop.hasAttribute('inert')).toBe(false);

    sidenav.collapse();
    fixture.detectChanges();
    expect(desktop.hasAttribute('inert')).toBe(true);

    sidenav.expand();
    fixture.detectChanges();
    expect(desktop.hasAttribute('inert')).toBe(false);
  }, IMPORT_TIMEOUT_MS);

  it('marks the off-canvas drawer inert while it animates closed', async () => {
    const fixture = await render();
    const sidenav = TestBed.inject(SidenavService);

    sidenav.open();
    fixture.detectChanges();
    // Open: the drawer and the desktop column are both mounted; the drawer is first.
    const drawer = panels(fixture.nativeElement)[0];
    expect(drawer.hasAttribute('inert')).toBe(false);

    sidenav.close();
    fixture.detectChanges();
    // Still mounted for the exit animation, but no longer reachable.
    expect(sidenav.isVisible()).toBe(true);
    expect(drawer.hasAttribute('inert')).toBe(true);
  }, IMPORT_TIMEOUT_MS);
});
