import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { ArtifactCardComponent } from './artifact-card.component';
import { ArtifactShareModalComponent } from './artifact-share-modal.component';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactHttpService } from '../../../../services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import { UserService } from '../../../../../auth/user.service';
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

  it('renders Share alongside Download', () => {
    expect(el().textContent).toContain('Share');
    expect(el().textContent).toContain('Download');
  });

  it('gives both actions a visible label inside their accessible name', () => {
    // WCAG 2.5.3: the visible label must be contained in the accessible
    // name. Both card actions are labelled, so neither needs a tooltip.
    expect(button(/^Share /).getAttribute('aria-label')).toContain('Share');
    expect(button(/^Download /).getAttribute('aria-label')).toContain(
      'Download',
    );
  });

  it('shares the version on the card, not the artifact head', () => {
    button(/^Share /).click();

    expect(dialog.open).toHaveBeenCalledTimes(1);
    const [component, config] = dialog.open.mock.calls[0];
    expect(component).toBe(ArtifactShareModalComponent);
    // The card renders one row per version, so the version it shares is
    // the one the user is looking at — a share pins an immutable version
    // and never follows HEAD.
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
        { provide: UserService, useValue: { currentUser: signal(null) } },
      ],
    });
    fixture = TestBed.createComponent(ArtifactCardComponent);
    fixture.componentRef.setInput('artifact', ARTIFACT);
    fixture.detectChanges();

    button(/^Share /).click();
    expect(dialog.open.mock.calls[0][1].data.ownerEmail).toBe('');
  });

  it('still opens the panel when the card body is clicked', () => {
    const hit = el().querySelector<HTMLButtonElement>('.artifact-card__hit');
    hit!.click();

    expect(artifactState.openArtifactPanel).toHaveBeenCalledWith({
      artifactId: 'art-1',
      version: 3,
      title: 'Quarterly Chart',
    });
    // Adding Share must not have stolen the card's primary action.
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('still downloads the version on the card', () => {
    button(/^Download /).click();
    expect(download.download).toHaveBeenCalledWith({
      artifactId: 'art-1',
      version: 3,
    });
  });

  it('keeps both action labels in the accessible name at any card width', () => {
    // On a narrow card (artifact panel docked open, split view, mobile) a
    // container query hides these labels visually so the title isn't
    // squeezed to nothing — but they are only *visually* hidden, never
    // removed, so the visible-label-in-accessible-name rule (WCAG 2.5.3)
    // still holds and the buttons keep their [appTooltip].
    expect(button(/^Share /).textContent).toContain('Share');
    expect(button(/^Download /).textContent).toContain('Download');
  });

  describe('rename and delete', () => {
    it('names no version on the whole-artifact actions', () => {
      // Share and Download really are scoped to this card's version and
      // say so. These two are not, so a matching "version 3" suffix
      // would promise something they do not do.
      expect(button(/^Rename /).getAttribute('aria-label')).toBe(
        'Rename artifact Quarterly Chart',
      );
      expect(button(/^Delete /).getAttribute('aria-label')).toBe(
        'Delete artifact Quarterly Chart and all versions',
      );
    });

    it('renames the whole artifact in local state', async () => {
      dialogResult = 'Renamed';
      button(/^Rename /).click();
      await flush();

      expect(dialog.open.mock.calls[0][0]).toBe(RenameArtifactDialogComponent);
      expect(http.renameArtifact).toHaveBeenCalledWith('art-1', 'Renamed');
      expect(artifactState.rename).toHaveBeenCalledWith('art-1', 'Renamed');
    });

    it('leaves state alone when a rename fails', async () => {
      dialogResult = 'Renamed';
      http.renameArtifact.mockRejectedValue(new Error('503'));
      button(/^Rename /).click();
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
      button(/^Delete /).click();
      await flush();

      expect(dialog.open.mock.calls[0][0]).toBe(ConfirmationDialogComponent);
      const data = dialog.open.mock.calls[0][1].data;
      expect(data.destructive).toBe(true);
      expect(data.message).toContain('all 3 versions');
      expect(data.message).toContain('not just the version on this card');
    });

    it('deletes the whole artifact and clears every sibling card', async () => {
      dialogResult = true;
      button(/^Delete /).click();
      await flush();

      expect(http.deleteArtifact).toHaveBeenCalledWith('art-1');
      // One registry call is what removes this card, its siblings for
      // the same artifact, and the docked panel.
      expect(artifactState.remove).toHaveBeenCalledWith('art-1');
    });

    it('does nothing when the confirmation is declined', async () => {
      dialogResult = false;
      button(/^Delete /).click();
      await flush();

      expect(http.deleteArtifact).not.toHaveBeenCalled();
      expect(artifactState.remove).not.toHaveBeenCalled();
    });

    it('keeps the card when the delete fails', async () => {
      dialogResult = true;
      http.deleteArtifact.mockRejectedValue(new Error('503'));
      button(/^Delete /).click();
      await flush();

      expect(artifactState.remove).not.toHaveBeenCalled();
      expect(toast.error).toHaveBeenCalled();
    });

    it('does not steal the card body click', () => {
      const hit = el().querySelector<HTMLButtonElement>('.artifact-card__hit');
      hit!.click();
      expect(artifactState.openArtifactPanel).toHaveBeenCalled();
      expect(dialog.open).not.toHaveBeenCalled();
    });
  });
});
