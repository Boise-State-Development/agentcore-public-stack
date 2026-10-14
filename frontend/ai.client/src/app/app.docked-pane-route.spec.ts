// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, RouterOutlet, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { Dialog } from '@angular/cdk/dialog';
import { TooltipDirective } from './components/tooltip/tooltip.directive';
import { SessionService } from './auth/session.service';
import { SessionService as SessionListService } from './session/services/session/session.service';
import { BrandingService } from '../branding/branding.service';
import { UserService } from './auth/user.service';
import { ToastService } from './services/toast/toast.service';
import { ArtifactStateService } from './session/services/artifacts/artifact-state.service';
import { ArtifactHttpService } from './session/services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from './session/services/artifacts/artifact-download.service';
import { FilePreviewStateService } from './session/services/file-preview/file-preview-state.service';
import { FilePreviewHttpService } from './session/services/file-preview/file-preview-http.service';
import { ArtifactPanelComponent } from './session/components/message-list/components/artifact/artifact-panel.component';
import { FilePreviewPanelComponent } from './session/components/message-list/components/file-preview/file-preview-panel.component';

// The shell is loaded through a dynamic import, as in app.spec.ts, which
// transforms the App graph on demand; give it the same budget.
const IMPORT_TIMEOUT_MS = 30_000;

@Component({ selector: 'app-sidenav', template: '' })
class StubSidenav {}

@Component({ selector: 'app-error-toast', template: '' })
class StubErrorToast {}

@Component({ selector: 'app-toast', template: '' })
class StubToast {}

@Component({ selector: 'app-background-task-toasts', template: '' })
class StubBackgroundTaskToasts {}

/** Stands in for the conversation page: what matters is that the real
 *  docked panes live inside the routed page and die with it. */
@Component({
  selector: 'app-test-conversation',
  imports: [ArtifactPanelComponent, FilePreviewPanelComponent],
  template: '<app-artifact-panel /><app-file-preview-panel />',
})
class TestConversationPage {}

@Component({ selector: 'app-test-project', template: '<h1>Project overview</h1>' })
class TestProjectPage {}

/**
 * Repro for the squashed project page: open the docked pane in a
 * conversation, client-side navigate to a page with no message list, and
 * the shell kept `artifact-pane-open` — reserving the pane's width as
 * right padding for a pane that was no longer rendered.
 */
describe('App shell: the docked-pane gutter does not outlive the conversation', () => {
  beforeEach(async () => {
    TestBed.resetTestingModule();
    const { App } = await import('./app');
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 's/:sessionId', component: TestConversationPage },
          { path: 'projects/:projectId/overview', component: TestProjectPage },
        ]),
        { provide: SessionService, useValue: { recheck: () => undefined } },
        {
          provide: SessionListService,
          useValue: {
            refreshSessions: () => undefined,
            currentSession: signal({ sessionId: 'sess-1' }),
          },
        },
        { provide: BrandingService, useValue: { pageTitle: 'Test' } },
        // Never resolves: the shell's gutter is the subject, not the pane body.
        { provide: ArtifactHttpService, useValue: { mintRenderToken: () => new Promise(() => undefined) } },
        { provide: ArtifactDownloadService, useValue: { download: vi.fn() } },
        { provide: FilePreviewHttpService, useValue: { fetchDocument: () => new Promise(() => undefined) } },
        { provide: UserService, useValue: { currentUser: signal({ email: 'owner@example.com' }) } },
        { provide: ToastService, useValue: { error: vi.fn() } },
        { provide: Dialog, useValue: { open: vi.fn(() => ({ closed: of(undefined) })) } },
      ],
    });
    TestBed.overrideComponent(App, {
      set: { imports: [RouterOutlet, TooltipDirective, StubSidenav, StubErrorToast, StubToast, StubBackgroundTaskToasts] },
    });
  }, IMPORT_TIMEOUT_MS);

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function renderAt(url: string) {
    const { App } = await import('./app');
    const fixture = TestBed.createComponent(App);
    await TestBed.inject(Router).navigateByUrl(url);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function gutter(root: HTMLElement): boolean {
    return root.querySelector('.artifact-pane-open') !== null;
  }

  it('drops the gutter when an open artifact pane is left for a project page', async () => {
    const fixture = await renderAt('/s/sess-1');
    const root = fixture.nativeElement as HTMLElement;
    TestBed.inject(ArtifactStateService).openArtifactPanel({ artifactId: 'a1', version: 1, title: 'Chart' });
    fixture.detectChanges();
    expect(gutter(root)).toBe(true);

    await TestBed.inject(Router).navigateByUrl('/projects/p1/overview');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(root.querySelector('h1')?.textContent).toBe('Project overview');
    expect(gutter(root)).toBe(false);
    expect(TestBed.inject(ArtifactStateService).openArtifact()).toBeNull();
  }, IMPORT_TIMEOUT_MS);

  it('drops the gutter when an open file preview is left for a project page', async () => {
    const fixture = await renderAt('/s/sess-1');
    const root = fixture.nativeElement as HTMLElement;
    TestBed.inject(FilePreviewStateService).open({ uploadId: 'up1', filename: 'plan.docx' });
    fixture.detectChanges();
    expect(gutter(root)).toBe(true);

    await TestBed.inject(Router).navigateByUrl('/projects/p1/overview');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(gutter(root)).toBe(false);
    expect(TestBed.inject(FilePreviewStateService).openFile()).toBeNull();
  }, IMPORT_TIMEOUT_MS);

  it('still opens and closes the pane in place on the conversation route', async () => {
    const fixture = await renderAt('/s/sess-1');
    const root = fixture.nativeElement as HTMLElement;
    const artifacts = TestBed.inject(ArtifactStateService);

    artifacts.openArtifactPanel({ artifactId: 'a1', version: 1, title: 'Chart' });
    fixture.detectChanges();
    expect(gutter(root)).toBe(true);

    // Handing the rail to a preview keeps the gutter; closing it clears it.
    TestBed.inject(FilePreviewStateService).open({ uploadId: 'up1', filename: 'plan.docx' });
    fixture.detectChanges();
    expect(gutter(root)).toBe(true);

    TestBed.inject(FilePreviewStateService).close();
    fixture.detectChanges();
    expect(gutter(root)).toBe(false);
  }, IMPORT_TIMEOUT_MS);
});
