import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { By } from '@angular/platform-browser';
import { of } from 'rxjs';
import { ArtifactCardComponent } from './artifact-card.component';
import { ArtifactShareModalComponent } from './artifact-share-modal.component';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactHttpService } from '../../../../services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import { ArtifactShareService } from '../../../../services/artifacts/artifact-share.service';
import { SessionService } from '../../../../services/session/session.service';
import { UserService } from '../../../../../auth/user.service';
import { ContentCardComponent } from '../../../../../components/content-card/content-card.component';
import { ToastService } from '../../../../../services/toast/toast.service';
import { ConfirmationDialogComponent } from '../../../../../components/confirmation-dialog';
import { RenameArtifactDialogComponent } from '../../../../../artifacts/components/rename-artifact-dialog.component';
import type { Artifact } from '../../../../services/artifacts/artifact.model';

const ARTIFACT: Artifact = {
  artifactId: 'art-1',
  version: 3,
  title: 'Quarterly Chart',
  contentType: 'text/html; charset=utf-8',
  updatedAt: '2026-09-03T00:00:00+00:00',
};

describe('ArtifactCardComponent', () => {
  let fixture: ComponentFixture<ArtifactCardComponent>;
  let dialog: { open: ReturnType<typeof vi.fn> };
  let artifactState: {
    openArtifactPanel: ReturnType<typeof vi.fn>;
    versionsFor: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
    rename: ReturnType<typeof vi.fn>;
  };
  let download: { download: ReturnType<typeof vi.fn> };
  let http: {
    renameArtifact: ReturnType<typeof vi.fn>;
    deleteArtifact: ReturnType<typeof vi.fn>;
    mintRenderToken: ReturnType<typeof vi.fn>;
  };
  let toast: { error: ReturnType<typeof vi.fn> };
  /** What the stubbed CDK dialog "returns" — the user's choice. */
  let dialogResult: unknown;

  async function flush(): Promise<void> {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  }

  const el = () => fixture.nativeElement as HTMLElement;
  const button = (label: RegExp): HTMLButtonElement => {
    const match = Array.from(el().querySelectorAll('button')).find((b) =>
      label.test(b.getAttribute('aria-label') ?? ''),
    );
    if (!match) throw new Error(`no button matching ${label}`);
    return match as HTMLButtonElement;
  };
  const card = (): ContentCardComponent =>
    fixture.debugElement.query(By.directive(ContentCardComponent))
      .componentInstance as ContentCardComponent;
  /** Picks an overflow-menu item, as the CDK menu would on activation. */
  const pick = (id: string): void => {
    card().menuSelect.emit(id);
  };
  const menuLabel = (id: string): string | undefined =>
    card()
      .menuItems()
      .find((item) => item.id === id)?.label;

  beforeEach(() => {
    TestBed.resetTestingModule();

    dialogResult = undefined;
    dialog = { open: vi.fn(() => ({ closed: of(dialogResult) })) };
    artifactState = {
      openArtifactPanel: vi.fn(),
      versionsFor: vi.fn(() => [ARTIFACT]),
      remove: vi.fn(),
      rename: vi.fn(),
    };
    download = { download: vi.fn().mockResolvedValue(true) };
    http = {
      renameArtifact: vi
        .fn()
        .mockResolvedValue({ artifactId: 'art-1', title: 'Renamed' }),
      deleteArtifact: vi.fn().mockResolvedValue(undefined),
      // The thumbnail's mint; rejecting leaves its placeholder standing.
      mintRenderToken: vi.fn().mockRejectedValue(new Error('no preview')),
    };
    toast = { error: vi.fn() };

    TestBed.configureTestingModule({
      imports: [ArtifactCardComponent],
      providers: [
        { provide: Dialog, useValue: dialog },
        { provide: ArtifactStateService, useValue: artifactState },
        { provide: ArtifactDownloadService, useValue: download },
        { provide: ArtifactHttpService, useValue: http },
        { provide: ToastService, useValue: toast },
        { provide: ArtifactShareService, useValue: {} },
        {
          provide: SessionService,
          useValue: { currentSession: signal({ sessionId: 'sess-1' }) },
        },
        {
          provide: UserService,
          useValue: { currentUser: signal({ email: 'owner@example.com' }) },
        },
      ],
    });

    fixture = TestBed.createComponent(ArtifactCardComponent);
    fixture.componentRef.setInput('artifact', ARTIFACT);
    fixture.detectChanges();
  });

  it('is drawn with the shared content card', () => {
    expect(card().title()).toBe('Quarterly Chart');
    expect(card().meta()).toBe('Web page · v3');
  });

  it('offers Open as the primary action, naming the version', () => {
    const open = button(/^Open /);
    expect(open.textContent?.trim()).toBe('Open');
    expect(open.getAttribute('aria-label')).toBe(
      'Open Web page artifact Quarterly Chart, version 3',
    );
  });

  it('opens the panel on Open', () => {
    button(/^Open /).click();

    expect(artifactState.openArtifactPanel).toHaveBeenCalledWith({
      artifactId: 'art-1',
      version: 3,
      title: 'Quarterly Chart',
    });
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('previews the version on the card, minted in this conversation', () => {
    expect(http.mintRenderToken).toHaveBeenCalledWith('art-1', 3, 'sess-1');
  });

  it('puts Share, Download, Rename and Delete in the overflow menu', () => {
    expect(card().menuItems().map((item) => item.id)).toEqual([
      'share',
      'download',
      'rename',
      'delete',
    ]);
    expect(button(/^More actions for /)).toBeTruthy();
    // Delete is the destructive one.
    expect(card().menuItems().find((item) => item.id === 'delete')?.danger).toBe(true);
  });

  it('scopes Share and Download to the version on the card', () => {
    // One card per version: a share pins an immutable version, and the
    // download is that version's bytes.
    expect(menuLabel('share')).toBe('Share version 3');
    expect(menuLabel('download')).toBe('Download version 3');
  });

  it('shares the version on the card, not the artifact head', () => {
    pick('share');

    expect(dialog.open).toHaveBeenCalledTimes(1);
    const [component, config] = dialog.open.mock.calls[0];
    expect(component).toBe(ArtifactShareModalComponent);
    expect(config.data).toEqual({
      artifactId: 'art-1',
      version: 3,
      title: 'Quarterly Chart',
      ownerEmail: 'owner@example.com',
    });
  });

  it('tolerates an unresolved user rather than blocking the dialog', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ArtifactCardComponent],
      providers: [
        { provide: Dialog, useValue: dialog },
        { provide: ArtifactStateService, useValue: artifactState },
        { provide: ArtifactDownloadService, useValue: download },
        { provide: ArtifactHttpService, useValue: http },
        { provide: ToastService, useValue: toast },
        { provide: ArtifactShareService, useValue: {} },
        {
          provide: SessionService,
          useValue: { currentSession: signal({ sessionId: 'sess-1' }) },
        },
        { provide: UserService, useValue: { currentUser: signal(null) } },
      ],
    });
    fixture = TestBed.createComponent(ArtifactCardComponent);
    fixture.componentRef.setInput('artifact', ARTIFACT);
    fixture.detectChanges();

    pick('share');
    expect(dialog.open.mock.calls[0][1].data.ownerEmail).toBe('');
  });

  it('downloads the version on the card and says so while it runs', async () => {
    let finish!: (ok: boolean) => void;
    download.download.mockReturnValue(
      new Promise<boolean>((resolve) => (finish = resolve)),
    );
    pick('download');
    fixture.detectChanges();

    expect(download.download).toHaveBeenCalledWith({
      artifactId: 'art-1',
      version: 3,
    });
    expect(card().status()).toBe('Downloading…');
    expect(card().live()).toBe(true);

    // A second pick while one is running is ignored.
    pick('download');
    expect(download.download).toHaveBeenCalledTimes(1);

    finish(true);
    await flush();
    fixture.detectChanges();
    expect(card().live()).toBe(false);
  });

  describe('rename and delete', () => {
    it('names no version on the whole-artifact actions', () => {
      // Share and Download really are scoped to this card's version and
      // say so. These two are not, so a matching "version 3" suffix
      // would promise something they do not do.
      expect(menuLabel('rename')).toBe('Rename artifact');
      expect(menuLabel('delete')).toBe('Delete artifact');
    });

    it('renames the whole artifact in local state', async () => {
      dialogResult = 'Renamed';
      pick('rename');
      await flush();

      expect(dialog.open.mock.calls[0][0]).toBe(RenameArtifactDialogComponent);
      expect(http.renameArtifact).toHaveBeenCalledWith('art-1', 'Renamed');
      expect(artifactState.rename).toHaveBeenCalledWith('art-1', 'Renamed');
    });

    it('leaves state alone when a rename fails', async () => {
      dialogResult = 'Renamed';
      http.renameArtifact.mockRejectedValue(new Error('503'));
      pick('rename');
      await flush();

      expect(artifactState.rename).not.toHaveBeenCalled();
      expect(toast.error).toHaveBeenCalled();
    });

    it('spells out the version count before deleting', async () => {
      // The card is captioned "v3" and sits beside version-scoped
      // buttons, so the dialog is the last chance to correct the reading
      // that this removes one version.
      artifactState.versionsFor.mockReturnValue([ARTIFACT, ARTIFACT, ARTIFACT]);
      dialogResult = false;
      pick('delete');
      await flush();

      expect(dialog.open.mock.calls[0][0]).toBe(ConfirmationDialogComponent);
      const data = dialog.open.mock.calls[0][1].data;
      expect(data.destructive).toBe(true);
      expect(data.message).toContain('all 3 versions');
      expect(data.message).toContain('not just the version on this card');
    });

    it('deletes the whole artifact and clears every sibling card', async () => {
      dialogResult = true;
      pick('delete');
      await flush();

      expect(http.deleteArtifact).toHaveBeenCalledWith('art-1');
      // One registry call is what removes this card, its siblings for
      // the same artifact, and the docked panel.
      expect(artifactState.remove).toHaveBeenCalledWith('art-1');
    });

    it('does nothing when the confirmation is declined', async () => {
      dialogResult = false;
      pick('delete');
      await flush();

      expect(http.deleteArtifact).not.toHaveBeenCalled();
      expect(artifactState.remove).not.toHaveBeenCalled();
    });

    it('keeps the card when the delete fails', async () => {
      dialogResult = true;
      http.deleteArtifact.mockRejectedValue(new Error('503'));
      pick('delete');
      await flush();

      expect(artifactState.remove).not.toHaveBeenCalled();
      expect(toast.error).toHaveBeenCalled();
    });

    it('ignores a second rename or delete while one is in flight', async () => {
      dialogResult = 'Renamed';
      let finish!: () => void;
      http.renameArtifact.mockReturnValue(
        new Promise((resolve) => (finish = () => resolve({ title: 'Renamed' }))),
      );
      pick('rename');
      await flush();
      pick('delete');
      pick('rename');
      await flush();

      expect(dialog.open).toHaveBeenCalledTimes(1);
      finish();
      await flush();
    });
  });
});
