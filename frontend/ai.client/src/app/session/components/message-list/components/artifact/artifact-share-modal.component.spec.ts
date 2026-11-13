import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog, DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { expectNamedDialog, openInCdkDialog } from '../../../../../../testing/cdk-dialog';
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
  followsLatest: false,
};

describe('ArtifactShareModalComponent', () => {
  let component: ArtifactShareModalComponent;
  let fixture: ComponentFixture<ArtifactShareModalComponent>;
  let shareService: {
    createShare: ReturnType<typeof vi.fn>;
    shareOptions: ReturnType<typeof vi.fn>;
    updateShare: ReturnType<typeof vi.fn>;
    revokeShare: ReturnType<typeof vi.fn>;
  };
  let dialogRef: { close: ReturnType<typeof vi.fn> };

  /** Reach past `protected` for interaction tests, matching the
   *  conversation share-modal spec's approach. */
  let originalClipboard: PropertyDescriptor | undefined;

  const api = () => component as unknown as Record<string, any>;
  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';

  beforeEach(() => {
    TestBed.resetTestingModule();

    shareService = {
      createShare: vi.fn(),
      shareOptions: vi.fn().mockResolvedValue({ shares: [], project: null }),
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

    stubClipboard();
  });

  afterEach(() => {
    fixture?.destroy();
    restoreClipboard();
  });
  /**
   * `navigator.clipboard` doesn't exist in jsdom, so it has to be
   * defined rather than stubbed — and `Object.defineProperty` is NOT
   * undone by the `vi.unstubAllGlobals()` backstop in test-setup.ts.
   * The builder runs vitest with `isolate: false`, so a leaked global
   * here would follow the worker into unrelated spec files and surface
   * as one randomly-chosen file timing out. Restore it explicitly.
   */
  function stubClipboard(writeText = vi.fn().mockResolvedValue(undefined)) {
    originalClipboard = Object.getOwnPropertyDescriptor(
      navigator,
      'clipboard',
    );
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
  }

  function restoreClipboard(): void {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard);
    } else {
      delete (navigator as unknown as Record<string, unknown>)['clipboard'];
    }
    originalClipboard = undefined;
  }

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
    shareService.shareOptions.mockResolvedValue({ shares: [{ ...SHARE, version: 1 }], project: null });
    await init();
    expect(shareService.shareOptions).toHaveBeenCalledWith('art-1');
    expect(text()).toContain('Existing links');
    expect(text()).toContain('Version 1');
  });

  it('still opens when the existing-links call fails', async () => {
    shareService.shareOptions.mockRejectedValue(new Error('boom'));
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
      false,
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

    expect(shareService.createShare).toHaveBeenCalledWith(
      'art-1',
      2,
      'specific',
      ['owner@example.com', 'friend@example.com'],
      false,
    );
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
    shareService.shareOptions.mockResolvedValue({ shares: [], project: null });
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
    shareService.shareOptions.mockResolvedValue({ shares: [SHARE], project: null });
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
    shareService.shareOptions.mockResolvedValue({ shares: [SHARE], project: null });
    await init();

    await api()['copyLink'](SHARE);
    fixture.detectChanges();

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      `${window.location.origin}/shared-artifact/share-1`,
    );
    expect(api()['copiedShareId']()).toBe('share-1');
  });

  it('falls back to a message when the clipboard is unavailable', async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error('denied')));
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
    shareService.shareOptions.mockResolvedValue({ shares: [SHARE], project: null });
    await init();

    await api()['revoke'](SHARE);
    api()['onClose']();

    // A revoke is a commit too — the opener needs to know something moved.
    expect(dialogRef.close).toHaveBeenCalledWith([]);
  });

  describe('an artifact made in a project task (3.3)', () => {
    const PROJECT = { projectId: 'prj_1', name: 'Enrollment Sync' };

    it('offers "Project members" first and selects it', async () => {
      shareService.shareOptions.mockResolvedValue({ shares: [], project: PROJECT });
      await init();
      const labels = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('fieldset label'))
        .map((l) => l.querySelector('span span')?.textContent?.trim());
      expect(labels).toEqual(['Project members', 'Public link', 'Limited share']);
      expect(text()).toContain('Everyone in Enrollment Sync can open it');
      expect(api()['selectedAccess']()).toBe('project');
    });

    it('keeps the choice the person already made', async () => {
      let resolve: (v: unknown) => void = () => undefined;
      shareService.shareOptions.mockReturnValue(new Promise((r) => (resolve = r)));
      fixture.detectChanges();
      api()['choose']('specific');
      resolve({ shares: [], project: PROJECT });
      await fixture.whenStable();
      expect(api()['selectedAccess']()).toBe('specific');
    });

    it('shares with the project and says where it is listed', async () => {
      shareService.shareOptions.mockResolvedValue({ shares: [], project: PROJECT });
      shareService.createShare.mockResolvedValue({ ...SHARE, accessLevel: 'project', projectId: 'prj_1' });
      await init();
      await api()['onShare']();
      fixture.detectChanges();
      expect(shareService.createShare).toHaveBeenCalledWith('art-1', 2, 'project', undefined, false);
      expect(text()).toContain('listed under Outputs on the project page');
    });

    it('shows the API sentence when the person left the project', async () => {
      shareService.shareOptions.mockResolvedValue({ shares: [], project: PROJECT });
      shareService.createShare.mockRejectedValue({ status: 403, error: { detail: "You are not a member of this artifact's project" } });
      await init();
      await api()['onShare']();
      fixture.detectChanges();
      expect(text()).toContain("You are not a member of this artifact's project");
    });

    it('labels an existing project link', async () => {
      shareService.shareOptions.mockResolvedValue({ shares: [{ ...SHARE, accessLevel: 'project' }], project: PROJECT });
      await init();
      expect(text()).toContain('Project members');
      expect(api()['audienceLabel']({ ...SHARE, accessLevel: 'project' })).toBe('Project members');
    });
  });
});

describe('ArtifactShareModalComponent: following the latest version', () => {
  let component: ArtifactShareModalComponent;
  let fixture: ComponentFixture<ArtifactShareModalComponent>;
  let shareService: {
    createShare: ReturnType<typeof vi.fn>;
    shareOptions: ReturnType<typeof vi.fn>;
    updateShare: ReturnType<typeof vi.fn>;
    revokeShare: ReturnType<typeof vi.fn>;
  };
  const api = () => component as unknown as Record<string, any>;
  const el = () => fixture.nativeElement as HTMLElement;
  const text = () => el().textContent ?? '';
  const followBox = () =>
    Array.from(el().querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))[0];

  beforeEach(() => {
    TestBed.resetTestingModule();
    shareService = {
      createShare: vi.fn(),
      shareOptions: vi.fn().mockResolvedValue({ shares: [], project: null }),
      updateShare: vi.fn(),
      revokeShare: vi.fn().mockResolvedValue(undefined),
    };
    TestBed.configureTestingModule({
      imports: [ArtifactShareModalComponent],
      providers: [
        { provide: ArtifactShareService, useValue: shareService },
        { provide: DIALOG_DATA, useValue: DATA },
        { provide: DialogRef, useValue: { close: vi.fn() } },
      ],
    });
    fixture = TestBed.createComponent(ArtifactShareModalComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => fixture?.destroy());

  async function init(): Promise<void> {
    await component.ngOnInit();
    fixture.detectChanges();
  }

  it('offers the option, unticked, and names the version a pinned link shows', async () => {
    await init();
    expect(text()).toContain('Always show the latest version');
    expect(text()).toContain('Leave this off to share version 2 only');
    expect(followBox().checked).toBe(false);
  });

  it('creates a following link when ticked and says what it opens', async () => {
    shareService.createShare.mockResolvedValue({ ...SHARE, followsLatest: true });
    await init();
    followBox().click();
    fixture.detectChanges();

    await api()['onShare']();
    fixture.detectChanges();

    expect(shareService.createShare).toHaveBeenCalledWith('art-1', 2, 'public', undefined, true);
    expect(text()).toContain('always opens the latest version');
    expect(text()).not.toContain("Later versions aren't included");
  });

  it('hides the option for project members and never sends it', async () => {
    shareService.shareOptions.mockResolvedValue({
      shares: [],
      project: { projectId: 'prj_1', name: 'Enrollment Sync' },
    });
    shareService.createShare.mockResolvedValue({ ...SHARE, accessLevel: 'project' });
    await init();
    api()['followsLatest'].set(true); // ticked on another option, then switched
    fixture.detectChanges();

    expect(text()).not.toContain('Always show the latest version');
    await api()['onShare']();
    expect(shareService.createShare).toHaveBeenCalledWith('art-1', 2, 'project', undefined, false);
  });

  it('labels an existing following link "Latest version" and offers no follow action on it', async () => {
    shareService.shareOptions.mockResolvedValue({
      shares: [{ ...SHARE, followsLatest: true }],
      project: null,
    });
    await init();
    expect(text()).toContain('Latest version');
    expect(text()).not.toContain('Follow latest');
  });

  it('switches an existing pinned link to follow the latest', async () => {
    shareService.shareOptions.mockResolvedValue({ shares: [SHARE], project: null });
    shareService.updateShare.mockResolvedValue({ ...SHARE, followsLatest: true });
    await init();
    expect(text()).toContain('Version 2');

    await api()['follow'](SHARE);
    fixture.detectChanges();

    expect(shareService.updateShare).toHaveBeenCalledWith('share-1', undefined, undefined, true);
    expect(text()).toContain('Latest version');
    expect(text()).not.toContain('Follow latest');
  });

  it('offers no follow action on an existing project link', async () => {
    shareService.shareOptions.mockResolvedValue({
      shares: [{ ...SHARE, accessLevel: 'project' }],
      project: null,
    });
    await init();
    expect(text()).not.toContain('Follow latest');
  });
});

describe('ArtifactShareModalComponent in a CDK dialog', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: ArtifactShareService,
          useValue: { shareOptions: vi.fn().mockResolvedValue({ shares: [], project: null }) },
        },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title, describes it with the version, and starts on the chosen access level', async () => {
    const { container } = await openInCdkDialog<ArtifactShareModalComponent, ArtifactShareModalData>(
      ArtifactShareModalComponent,
      { data: DATA },
    );
    expectNamedDialog(container, { name: 'Share artifact', description: 'Quarterly Chart · version 2' });
    const initial = container.querySelector<HTMLInputElement>('[cdkFocusInitial]');
    expect(initial?.type).toBe('radio');
    expect(initial?.value).toBe('public');
  });
});
