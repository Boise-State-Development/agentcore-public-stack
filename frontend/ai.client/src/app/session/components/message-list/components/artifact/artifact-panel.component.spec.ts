import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { ArtifactPanelComponent } from './artifact-panel.component';
import { ArtifactViewerComponent } from './artifact-viewer.component';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactHttpService } from '../../../../services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import { SessionService } from '../../../../services/session/session.service';
import { UserService } from '../../../../../auth/user.service';
import { ToastService } from '../../../../../services/toast/toast.service';
import { of } from 'rxjs';

/**
 * Guards the PR-3 extraction: the panel's viewer body moved into
 * `ArtifactViewerComponent`, and the panel must keep driving it with the
 * same state it used to render inline. The panel had no spec before, so
 * these assertions exist specifically so that refactor can't silently
 * regress the owner's view.
 */
describe('ArtifactPanelComponent', () => {
  let fixture: ComponentFixture<ArtifactPanelComponent>;
  let http: {
    mintRenderToken: ReturnType<typeof vi.fn>;
    getArtifactContent: ReturnType<typeof vi.fn>;
    renameArtifact: ReturnType<typeof vi.fn>;
    deleteArtifact: ReturnType<typeof vi.fn>;
  };
  let state: {
    remove: ReturnType<typeof vi.fn>;
    rename: ReturnType<typeof vi.fn>;
  };
  let toast: { error: ReturnType<typeof vi.fn> };
  /** What the stubbed CDK dialog "returns" — the user's choice. */
  let dialogResult: unknown;

  const OPEN = { artifactId: 'art-1', version: 2, title: 'Chart' };
  const openArtifact = signal<typeof OPEN | null>(OPEN);

  const el = () => fixture.nativeElement as HTMLElement;
  /** The child instance, so we can read what the panel handed down. */
  const viewerInstance = (): ArtifactViewerComponent | null => {
    const de = fixture.debugElement.query(
      (n) => n.componentInstance instanceof ArtifactViewerComponent,
    );
    return de ? (de.componentInstance as ArtifactViewerComponent) : null;
  };

  async function flush(): Promise<void> {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  }

  beforeEach(async () => {
    openArtifact.set(OPEN);
    http = {
      mintRenderToken: vi
        .fn()
        .mockResolvedValue({ url: 'https://artifacts.x/?t=jwt', expiresAt: 'e' }),
      getArtifactContent: vi.fn().mockResolvedValue({
        content: 'source text',
        contentType: 'text/html',
        version: 2,
      }),
      renameArtifact: vi
        .fn()
        .mockResolvedValue({ artifactId: 'art-1', title: 'Renamed' }),
      deleteArtifact: vi.fn().mockResolvedValue(undefined),
    };
    state = { remove: vi.fn(), rename: vi.fn() };
    toast = { error: vi.fn() };
    dialogResult = undefined;

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ArtifactPanelComponent],
      providers: [
        {
          provide: ArtifactStateService,
          useValue: {
            openArtifact,
            paneWidth: signal(672),
            paneWidthMin: 360,
            paneWidthMax: 1200,
            versionsFor: () => [],
            openArtifactPanel: vi.fn(),
            closeArtifactPanel: vi.fn(),
            setPaneWidth: vi.fn(),
            remove: state.remove,
            rename: state.rename,
          },
        },
        { provide: ArtifactHttpService, useValue: http },
        { provide: ArtifactDownloadService, useValue: { download: vi.fn() } },
        {
          provide: SessionService,
          useValue: { currentSession: signal({ sessionId: 'sess-1' }) },
        },
        {
          provide: UserService,
          useValue: { currentUser: signal({ email: 'owner@example.com' }) },
        },
        { provide: ToastService, useValue: toast },
        {
          provide: Dialog,
          useValue: { open: vi.fn(() => ({ closed: of(dialogResult) })) },
        },
      ],
    });

    fixture = TestBed.createComponent(ArtifactPanelComponent);
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture?.destroy();
  });

  it('renders the extracted viewer rather than its own iframe markup', () => {
    expect(viewerInstance()).not.toBeNull();
  });

  it('hands the viewer the minted URL and the artifact title', () => {
    expect(http.mintRenderToken).toHaveBeenCalledWith('art-1', 2, 'sess-1');
    expect(viewerInstance()!.safeUrl()).not.toBeNull();
    expect(viewerInstance()!.title()).toBe('Chart');
  });

  it('starts in preview with the skeleton showing', () => {
    const v = viewerInstance()!;
    expect(v.view()).toBe('preview');
    // The iframe hasn't fired `load` yet, so the skeleton must remain.
    expect(v.previewReady()).toBe(false);
    expect(el().querySelector('[role="status"]')).not.toBeNull();
  });

  it('clears the skeleton once the viewer reports the iframe painted', () => {
    viewerInstance()!.iframeLoad.emit();
    fixture.detectChanges();

    expect(viewerInstance()!.previewReady()).toBe(true);
    expect(el().querySelector('[role="status"]')).toBeNull();
  });

  it('fetches source and switches the viewer to code view', async () => {
    const codeButton = Array.from(el().querySelectorAll('button')).find(
      (b) => b.getAttribute('aria-label') === 'View code',
    )!;
    codeButton.click();
    await flush();
    fixture.detectChanges();

    expect(http.getArtifactContent).toHaveBeenCalledWith('art-1', 2);
    expect(viewerInstance()!.view()).toBe('code');
    expect(viewerInstance()!.source()?.content).toBe('source text');
  });

  it('surfaces a mint failure through the viewer, retryable', async () => {
    http.mintRenderToken.mockRejectedValue(new Error('nope'));
    (fixture.componentInstance as unknown as Record<string, any>)['retry']();
    await flush();
    fixture.detectChanges();

    expect(viewerInstance()!.error()).toBeTruthy();
    expect(viewerInstance()!.safeUrl()).toBeNull();
  });

  it('marks the viewer inert only while the resize handle is dragged', () => {
    const panel = fixture.componentInstance as unknown as Record<string, any>;
    expect(viewerInstance()!.inert()).toBe(false);

    panel['dragging'].set(true);
    fixture.detectChanges();
    // The iframe would otherwise swallow the pointer stream mid-drag.
    expect(viewerInstance()!.inert()).toBe(true);
  });

  describe('rename and delete', () => {
    const panel = () =>
      fixture.componentInstance as unknown as Record<string, any>;

    it('applies a rename to the whole artifact in local state', async () => {
      // The backend retitles every version row, so the registry has to
      // follow — otherwise the version picker lists one artifact under
      // two names.
      dialogResult = 'Renamed';
      await panel()['rename'](OPEN);

      expect(http.renameArtifact).toHaveBeenCalledWith('art-1', 'Renamed');
      expect(state.rename).toHaveBeenCalledWith('art-1', 'Renamed');
    });

    it('leaves state untouched when a rename fails', async () => {
      dialogResult = 'Renamed';
      http.renameArtifact.mockRejectedValue(new Error('503'));

      await panel()['rename'](OPEN);

      expect(state.rename).not.toHaveBeenCalled();
      expect(toast.error).toHaveBeenCalled();
    });

    it('removes the artifact from state once the delete succeeds', async () => {
      // Removal is what clears the inline cards *and* closes this panel;
      // both read the same registry.
      dialogResult = true;
      await panel()['confirmDelete'](OPEN);

      expect(http.deleteArtifact).toHaveBeenCalledWith('art-1');
      expect(state.remove).toHaveBeenCalledWith('art-1');
    });

    it('does not delete when the confirmation is declined', async () => {
      dialogResult = false;
      await panel()['confirmDelete'](OPEN);

      expect(http.deleteArtifact).not.toHaveBeenCalled();
      expect(state.remove).not.toHaveBeenCalled();
    });

    it('keeps the artifact on screen when the delete fails', async () => {
      // An optimistic removal would look like success and then reappear
      // on the next session load.
      dialogResult = true;
      http.deleteArtifact.mockRejectedValue(new Error('503'));

      await panel()['confirmDelete'](OPEN);

      expect(state.remove).not.toHaveBeenCalled();
      expect(toast.error).toHaveBeenCalled();
    });
  });
});
