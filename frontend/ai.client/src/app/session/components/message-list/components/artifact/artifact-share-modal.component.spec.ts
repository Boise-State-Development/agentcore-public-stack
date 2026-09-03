import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import {
  ArtifactShareModalComponent,
  type ArtifactShareModalData,
} from './artifact-share-modal.component';
import {
  ArtifactShareService,
  type ArtifactShare,
} from '../../../../services/artifacts/artifact-share.service';

const DATA: ArtifactShareModalData = {
  artifactId: 'art-1',
  version: 2,
  title: 'Quarterly Chart',
  ownerEmail: 'owner@example.com',
};

const SHARE: ArtifactShare = {
  shareId: 'share-1',
  artifactId: 'art-1',
  version: 2,
  ownerId: 'owner-1',
  accessLevel: 'public',
  title: 'Quarterly Chart',
  contentType: 'text/html; charset=utf-8',
  createdAt: '2026-09-03T00:00:00+00:00',
  shareUrl: '/shared-artifact/share-1',
};

describe('ArtifactShareModalComponent', () => {
  let component: ArtifactShareModalComponent;
  let fixture: ComponentFixture<ArtifactShareModalComponent>;
  let shareService: {
    createShare: ReturnType<typeof vi.fn>;
    listShares: ReturnType<typeof vi.fn>;
    updateShare: ReturnType<typeof vi.fn>;
    revokeShare: ReturnType<typeof vi.fn>;
  };
  let dialogRef: { close: ReturnType<typeof vi.fn> };

  /** Reach past `protected` for interaction tests, matching the
   *  conversation share-modal spec's approach. */
  const api = () => component as unknown as Record<string, any>;
  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';

  beforeEach(() => {
    TestBed.resetTestingModule();

    shareService = {
      createShare: vi.fn(),
      listShares: vi.fn().mockResolvedValue([]),
      updateShare: vi.fn(),
      revokeShare: vi.fn().mockResolvedValue(undefined),
    };
    dialogRef = { close: vi.fn() };

    TestBed.configureTestingModule({
      imports: [ArtifactShareModalComponent],
      providers: [
        // DI token overrides, not vi.mock — module mocks leak across specs.
        { provide: ArtifactShareService, useValue: shareService },
        { provide: DIALOG_DATA, useValue: DATA },
        { provide: DialogRef, useValue: dialogRef },
      ],
    });

    fixture = TestBed.createComponent(ArtifactShareModalComponent);
    component = fixture.componentInstance;

    // jsdom has no clipboard; the copy path is exercised explicitly below.
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
  });

  async function init(): Promise<void> {
    await component.ngOnInit();
    fixture.detectChanges();
  }

  // ----------------------------------------------------------------
  // Rendering
  // ----------------------------------------------------------------

  it('creates the component', () => {
    expect(component).toBeTruthy();
  });

  it('names the artifact and the version being shared', async () => {
    await init();
    expect(text()).toContain('Quarterly Chart');
    expect(text()).toContain('version 2');
  });

  it('offers exactly the public and limited access levels', async () => {
    await init();
    expect(text()).toContain('Public link');
    expect(text()).toContain('Limited share');
    expect(text()).not.toContain('Keep private');
  });

  it('loads existing links on open', async () => {
    shareService.listShares.mockResolvedValue([{ ...SHARE, version: 1 }]);
    await init();
    expect(shareService.listShares).toHaveBeenCalledWith('art-1');
    expect(text()).toContain('Existing links');
    expect(text()).toContain('Version 1');
  });

  it('still opens when the existing-links call fails', async () => {
    shareService.listShares.mockRejectedValue(new Error('boom'));
    await init();
    // The list is a convenience; failing it must not block sharing.
    expect(text()).toContain('Create share link');
    expect(api()['error']()).toBeNull();
  });

  // ----------------------------------------------------------------
  // Create
  // ----------------------------------------------------------------

  it('creates a share pinned to the version it was opened for', async () => {
    shareService.createShare.mockResolvedValue(SHARE);
    await init();

    await api()['onShare']();
    fixture.detectChanges();

    expect(shareService.createShare).toHaveBeenCalledWith(
      'art-1',
      2,
      'public',
      undefined,
    );
  });

  it('sends the owner plus the added emails for a limited share', async () => {
    shareService.createShare.mockResolvedValue({
      ...SHARE,
      accessLevel: 'specific',
    });
    await init();

    api()['selectedAccess'].set('specific');
    api()['emailInput'].set('friend@example.com');
    api()['addEmail']();
    await api()['onShare']();

    expect(shareService.createShare).toHaveBeenCalledWith('art-1', 2, 'specific', [
      'owner@example.com',
      'friend@example.com',
    ]);
  });

  it('says the link is pinned to one version after sharing', async () => {
    shareService.createShare.mockResolvedValue(SHARE);
    await init();

    await api()['onShare']();
    fixture.detectChanges();

    expect(text()).toContain('Artifact shared');
    // The pinning promise is the whole consent model — it must be stated.
    expect(text()).toContain('always shows version 2');
    expect(text()).toContain("Later versions aren't included");
  });

  it('does not list the just-created link twice', async () => {
    shareService.listShares.mockResolvedValue([]);
    shareService.createShare.mockResolvedValue(SHARE);
    await init();

    await api()['onShare']();
    fixture.detectChanges();

    expect(api()['shares']()).toHaveLength(1);
    expect(api()['otherShares']()).toHaveLength(0);
    expect(text()).not.toContain('Existing links');
  });

  // ----------------------------------------------------------------
  // Email chips
  // ----------------------------------------------------------------

  it('shows the owner as a non-removable chip for a limited share', async () => {
    await init();
    api()['selectedAccess'].set('specific');
    fixture.detectChanges();

    expect(text()).toContain('People with access');
    expect(text()).toContain('owner@example.com (you)');
  });

  it('adds and removes emails', async () => {
    await init();
    api()['selectedAccess'].set('specific');
    api()['emailInput'].set('friend@example.com');
    api()['addEmail']();
    fixture.detectChanges();
    expect(text()).toContain('friend@example.com');

    api()['removeEmail']('friend@example.com');
    fixture.detectChanges();
    expect(text()).not.toContain('friend@example.com');
  });

  it('rejects a malformed, duplicate, or owner email', async () => {
    await init();
    api()['selectedAccess'].set('specific');

    api()['emailInput'].set('not-an-email');
    api()['addEmail']();
    api()['emailInput'].set('owner@example.com');
    api()['addEmail']();
    api()['emailInput'].set('friend@example.com');
    api()['addEmail']();
    api()['emailInput'].set('friend@example.com');
    api()['addEmail']();

    expect(api()['allowedEmails']()).toEqual(['friend@example.com']);
  });

  // ----------------------------------------------------------------
  // Revoke
  // ----------------------------------------------------------------

  it('revokes a link and drops it from the list', async () => {
    shareService.listShares.mockResolvedValue([SHARE]);
    await init();

    await api()['revoke'](SHARE);
    fixture.detectChanges();

    expect(shareService.revokeShare).toHaveBeenCalledWith('share-1');
    expect(api()['shares']()).toEqual([]);
  });

  it('retires the result panel when the link it shows is revoked', async () => {
    shareService.createShare.mockResolvedValue(SHARE);
    await init();
    await api()['onShare']();

    await api()['revoke'](SHARE);
    fixture.detectChanges();

    // Otherwise the dialog would keep offering a dead link to copy.
    expect(api()['shareResult']()).toBeNull();
    expect(text()).not.toContain('Artifact shared');
  });

  // ----------------------------------------------------------------
  // Link building + copy
  // ----------------------------------------------------------------

  it('builds an absolute link from the server-supplied route', async () => {
    await init();
    expect(api()['absoluteUrl'](SHARE)).toBe(
      `${window.location.origin}/shared-artifact/share-1`,
    );
  });

  it('copies the absolute link and flags which link was copied', async () => {
    shareService.listShares.mockResolvedValue([SHARE]);
    await init();

    await api()['copyLink'](SHARE);
    fixture.detectChanges();

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      `${window.location.origin}/shared-artifact/share-1`,
    );
    expect(api()['copiedShareId']()).toBe('share-1');
  });

  it('falls back to a message when the clipboard is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    });
    await init();

    await api()['copyLink'](SHARE);
    fixture.detectChanges();

    expect(text()).toContain('Could not copy automatically');
  });

  // ----------------------------------------------------------------
  // Errors
  // ----------------------------------------------------------------

  it.each([
    [404, 'That artifact version no longer exists.'],
    [403, 'You do not have permission to change this share.'],
    [503, 'Sharing is temporarily unavailable. Please try again.'],
  ])('explains a %i from the share API', async (status, expected) => {
    shareService.createShare.mockRejectedValue({ status });
    await init();

    await api()['onShare']();
    fixture.detectChanges();

    expect(text()).toContain(expected);
  });

  it('surfaces a backend detail message when there is one', async () => {
    shareService.createShare.mockRejectedValue({
      status: 400,
      error: { detail: 'Something specific went wrong' },
    });
    await init();

    await api()['onShare']();
    fixture.detectChanges();

    expect(text()).toContain('Something specific went wrong');
  });

  it('falls back to a generic message for an unrecognized failure', async () => {
    shareService.createShare.mockRejectedValue(new Error('offline'));
    await init();

    await api()['onShare']();
    fixture.detectChanges();

    expect(text()).toContain('Failed to create share');
  });

  // ----------------------------------------------------------------
  // Close contract
  // ----------------------------------------------------------------

  it('closes with undefined when nothing was committed', async () => {
    await init();
    api()['onClose']();
    // `undefined` means cancelled, per the dialog convention.
    expect(dialogRef.close).toHaveBeenCalledWith(undefined);
  });

  it('closes with the current links after creating one', async () => {
    shareService.createShare.mockResolvedValue(SHARE);
    await init();

    await api()['onShare']();
    api()['onClose']();

    expect(dialogRef.close).toHaveBeenCalledWith([SHARE]);
  });

  it('closes with a result after a revoke, even with nothing created', async () => {
    shareService.listShares.mockResolvedValue([SHARE]);
    await init();

    await api()['revoke'](SHARE);
    api()['onClose']();

    // A revoke is a commit too — the opener needs to know something moved.
    expect(dialogRef.close).toHaveBeenCalledWith([]);
  });
});
