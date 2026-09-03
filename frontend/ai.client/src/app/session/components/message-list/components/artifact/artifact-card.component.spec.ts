import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { ArtifactCardComponent } from './artifact-card.component';
import { ArtifactShareModalComponent } from './artifact-share-modal.component';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import { UserService } from '../../../../../auth/user.service';
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
  let artifactState: { openArtifactPanel: ReturnType<typeof vi.fn> };
  let download: { download: ReturnType<typeof vi.fn> };

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

    dialog = { open: vi.fn() };
    artifactState = { openArtifactPanel: vi.fn() };
    download = { download: vi.fn().mockResolvedValue(true) };

    TestBed.configureTestingModule({
      imports: [ArtifactCardComponent],
      providers: [
        { provide: Dialog, useValue: dialog },
        { provide: ArtifactStateService, useValue: artifactState },
        { provide: ArtifactDownloadService, useValue: download },
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
});
