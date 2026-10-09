import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog, DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { ShareModalComponent, ShareModalData } from './share-modal.component';
import { ShareService, ShareResponse, ShareListResponse } from '../../services/share/share.service';
import { ProjectApiService } from '../../../projects/services/project-api.service';
import { of, throwError } from 'rxjs';

describe('ShareModalComponent', () => {
  let component: ShareModalComponent;
  let fixture: ComponentFixture<ShareModalComponent>;
  let mockShareService: any;
  let mockDialogRef: any;

  const mockDialogData: ShareModalData = {
    sessionId: 'sess-001',
    ownerEmail: 'owner@example.com',
  };

  const mockShareResponse: ShareResponse = {
    shareId: 'share-001',
    sessionId: 'sess-001',
    ownerId: 'user-001',
    accessLevel: 'public',
    createdAt: '2025-06-01T00:00:00Z',
    shareUrl: '/shared/share-001',
  };

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockShareService = {
      createShare: vi.fn(),
      listSharesForSession: vi.fn().mockResolvedValue({ shares: [] } as ShareListResponse),
      updateShare: vi.fn(),
      revokeShare: vi.fn(),
      exportSharedConversation: vi.fn(),
    };

    mockDialogRef = {
      close: vi.fn(),
    };

    TestBed.configureTestingModule({
      imports: [ShareModalComponent],
      providers: [
        { provide: ShareService, useValue: mockShareService },
        { provide: ProjectApiService, useValue: { members: vi.fn() } },
        { provide: DIALOG_DATA, useValue: mockDialogData },
        { provide: DialogRef, useValue: mockDialogRef },
      ],
    });

    fixture = TestBed.createComponent(ShareModalComponent);
    component = fixture.componentInstance;
  });

  // -----------------------------------------------------------------------
  // Modal opens
  // -----------------------------------------------------------------------

  it('should create the component', () => {
    expect(component).toBeTruthy();
  });

  // -----------------------------------------------------------------------
  // Two access options displayed (no private)
  // -----------------------------------------------------------------------

  it('should display two access level options (public and limited)', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Public link');
    expect(el.textContent).toContain('Limited share');
    expect(el.textContent).not.toContain('Keep private');
  });

  // -----------------------------------------------------------------------
  // Create share link calls service
  // -----------------------------------------------------------------------

  it('should call createShare on share button click', async () => {
    mockShareService.createShare.mockResolvedValue(mockShareResponse);
    await component.ngOnInit();
    fixture.detectChanges();

    await (component as any).onShare();
    fixture.detectChanges();

    expect(mockShareService.createShare).toHaveBeenCalledWith(
      'sess-001',
      'public',
      undefined,
      { suppressErrorToast: true },
    );
  });

  // -----------------------------------------------------------------------
  // Confirmation with "Future messages aren't included"
  // -----------------------------------------------------------------------

  it('should display confirmation after successful share', async () => {
    mockShareService.createShare.mockResolvedValue(mockShareResponse);
    await component.ngOnInit();
    fixture.detectChanges();

    await (component as any).onShare();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Chat shared');
    expect(el.textContent).toContain("Future messages aren't included");
  });

  // -----------------------------------------------------------------------
  // Error display on failure
  // -----------------------------------------------------------------------

  it('should display error message on share failure', async () => {
    mockShareService.createShare.mockRejectedValue({
      error: { detail: 'Something went wrong' },
    });
    await component.ngOnInit();
    fixture.detectChanges();

    await (component as any).onShare();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Something went wrong');
  });

  // -----------------------------------------------------------------------
  // Email input for specific access
  // -----------------------------------------------------------------------

  it('should show email input when specific access is selected', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('People with access');
    expect(el.textContent).toContain('owner@example.com');
  });

  // -----------------------------------------------------------------------
  // Remove email from allowed list
  // -----------------------------------------------------------------------

  it('should add and remove emails from allowed list', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    (component as any).emailInput.set('friend@example.com');
    (component as any).addEmail();
    fixture.detectChanges();

    let el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('friend@example.com');

    (component as any).removeEmail('friend@example.com');
    fixture.detectChanges();

    el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).not.toContain('friend@example.com');
  });

  // -----------------------------------------------------------------------
  // Existing shares info (multiple shares)
  // -----------------------------------------------------------------------

  it('should display existing shares count when shares exist', async () => {
    mockShareService.listSharesForSession.mockResolvedValue({
      shares: [mockShareResponse, { ...mockShareResponse, shareId: 'share-002' }],
    } as ShareListResponse);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('2 existing shares');
  });

  it('should say a project re-share replaces the project snapshot', async () => {
    (component as any).selectedAccess.set('project');
    mockShareService.listSharesForSession.mockResolvedValue({
      shares: [{ ...mockShareResponse, accessLevel: 'project' }],
    } as ShareListResponse);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Sharing again replaces the snapshot the project sees');
    expect(el.textContent).not.toContain('add another snapshot');
  });

  it('should keep the add-another copy for a public share of a task already shared with the project', async () => {
    (component as any).selectedAccess.set('public');
    mockShareService.listSharesForSession.mockResolvedValue({
      shares: [{ ...mockShareResponse, accessLevel: 'project' }],
    } as ShareListResponse);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('1 existing share');
    expect(el.textContent).toContain('add another snapshot');
  });

  it('should not show existing shares info when no shares exist', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).not.toContain('existing share');
  });

  // -----------------------------------------------------------------------
  // Close behavior
  // -----------------------------------------------------------------------

  it('should close dialog on onClose', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).onClose();

    expect(mockDialogRef.close).toHaveBeenCalledWith(false);
  });

  it('should close dialog with true after successful share', async () => {
    mockShareService.createShare.mockResolvedValue(mockShareResponse);
    await component.ngOnInit();
    fixture.detectChanges();

    await (component as any).onShare();
    (component as any).onClose();

    expect(mockDialogRef.close).toHaveBeenCalledWith(true);
  });

  // -----------------------------------------------------------------------
  // Validation: canSubmit
  // -----------------------------------------------------------------------

  it('should enable submit when specific access has no additional emails (owner-only share)', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    fixture.detectChanges();

    // Owner email is always included, so sharing with just yourself is valid
    expect((component as any).canSubmit()).toBe(true);
  });

  it('should enable submit when specific access has additional emails', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    (component as any).emailInput.set('friend@example.com');
    (component as any).addEmail();
    fixture.detectChanges();

    expect((component as any).canSubmit()).toBe(true);
  });

  it('should enable submit for public access without emails', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('public');
    fixture.detectChanges();

    expect((component as any).canSubmit()).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Email validation
  // -----------------------------------------------------------------------

  it('should not add invalid email', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    (component as any).emailInput.set('not-an-email');
    (component as any).addEmail();

    expect((component as any).allowedEmails().length).toBe(0);
  });

  it('should not add owner email to allowed list', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    (component as any).emailInput.set('owner@example.com');
    (component as any).addEmail();

    expect((component as any).allowedEmails().length).toBe(0);
  });

  it('should not add duplicate email', async () => {
    await component.ngOnInit();
    fixture.detectChanges();

    (component as any).selectedAccess.set('specific');
    (component as any).emailInput.set('friend@example.com');
    (component as any).addEmail();
    (component as any).emailInput.set('friend@example.com');
    (component as any).addEmail();

    expect((component as any).allowedEmails().length).toBe(1);
  });

  // -----------------------------------------------------------------------
  // Multiple shares: new share appends to existing list
  // -----------------------------------------------------------------------

  it('should append new share to existing shares list', async () => {
    mockShareService.listSharesForSession.mockResolvedValue({
      shares: [mockShareResponse],
    } as ShareListResponse);

    const newShare = { ...mockShareResponse, shareId: 'share-002' };
    mockShareService.createShare.mockResolvedValue(newShare);

    await component.ngOnInit();
    fixture.detectChanges();

    await (component as any).onShare();
    fixture.detectChanges();

    expect((component as any).existingShares().length).toBe(2);
  });

  it('does not offer "Project members" for a task outside a project', async () => {
    await component.ngOnInit();
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent).not.toContain('Project members');
  });
});

describe('ShareModalComponent (a task in a project)', () => {
  let fixture: ComponentFixture<ShareModalComponent>;
  let component: ShareModalComponent;
  const shareService = {
    createShare: vi.fn(),
    listSharesForSession: vi.fn(),
  };
  const projectApi = { members: vi.fn() };
  const MEMBERS = [
    { email: 'owner@x.edu', name: 'Olive Owner', role: 'owner', hasSignedIn: true },
    { email: 'me@x.edu', name: 'Me', role: 'editor', hasSignedIn: true },
    { email: 'ann@x.edu', name: 'Ann Lee', role: 'viewer', hasSignedIn: true },
    { email: 'new@x.edu', name: null, role: 'viewer', hasSignedIn: false },
  ];
  const RESULT = {
    shareId: 'sh_1', sessionId: 'sess-9', ownerId: 'u', accessLevel: 'project', projectId: 'prj_1',
    createdAt: '2026-09-24T00:00:00Z', shareUrl: '/shared/sh_1',
  } as ShareResponse;
  const el = () => fixture.nativeElement as HTMLElement;
  const radio = (name: string, value: string) =>
    el().querySelector<HTMLInputElement>(`input[name=${name}][value=${value}]`)!;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    shareService.listSharesForSession.mockResolvedValue({ shares: [] });
    projectApi.members.mockReturnValue(of({ members: MEMBERS, canManage: false }));
    TestBed.configureTestingModule({
      imports: [ShareModalComponent],
      providers: [
        { provide: ShareService, useValue: shareService },
        { provide: ProjectApiService, useValue: projectApi },
        { provide: DIALOG_DATA, useValue: { sessionId: 'sess-9', ownerEmail: 'me@x.edu', projectId: 'prj_1' } as ShareModalData },
        { provide: DialogRef, useValue: { close: vi.fn() } },
      ],
    });
    fixture = TestBed.createComponent(ShareModalComponent);
    component = fixture.componentInstance;
    await component.ngOnInit();
    fixture.detectChanges();
  });

  it('offers "Project members" first and selects it', () => {
    const labels = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('label span')).map(
      l => l.textContent?.trim(),
    );
    expect(labels).toEqual(['Project members', 'Public link', 'Limited share']);
    const checked = (fixture.nativeElement as HTMLElement).querySelector<HTMLInputElement>('input[type=radio]:checked');
    expect(checked?.value).toBe('project');
  });

  it('shares to the project and says where it is listed', async () => {
    shareService.createShare.mockResolvedValue({
      shareId: 'sh_1', sessionId: 'sess-9', ownerId: 'u', accessLevel: 'project', projectId: 'prj_1',
      createdAt: '2026-09-24T00:00:00Z', shareUrl: '/shared/sh_1',
    } as ShareResponse);
    await (component as any).onShare();
    fixture.detectChanges();
    expect(shareService.createShare).toHaveBeenCalledWith('sess-9', 'project', undefined, { suppressErrorToast: true });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Shared with the project');
    expect(text).toContain('Shared tasks');
  });

  it.each([
    [403, 'You are no longer a member of this project.'],
    [409, 'This project is archived. Restore it to make changes.'],
  ])('shows the API detail on a %s', async (status, detail) => {
    shareService.createShare.mockRejectedValue({ status, error: { detail } });
    await (component as any).onShare();
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(detail);
  });

  describe('letting people know (2.5b)', () => {
    it('tells nobody by default and loads no members', async () => {
      shareService.createShare.mockResolvedValue(RESULT);
      expect(radio('notifyMode', 'none').checked).toBe(true);
      await (component as any).onShare();
      expect(projectApi.members).not.toHaveBeenCalled();
      expect(shareService.createShare.mock.calls[0][3].notify).toBeUndefined();
    });

    it('sends everyone and the trimmed note', async () => {
      shareService.createShare.mockResolvedValue(RESULT);
      radio('notifyMode', 'all').click();
      const note = el().querySelector<HTMLTextAreaElement>('#share-note')!;
      note.value = '  Can you take the vendor reply?  ';
      note.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      await (component as any).onShare();
      fixture.detectChanges();
      expect(shareService.createShare).toHaveBeenCalledWith('sess-9', 'project', undefined, {
        suppressErrorToast: true,
        notify: { all: true },
        note: 'Can you take the vendor reply?',
      });
      expect(el().textContent).toContain('Everyone in the project will be notified.');
    });

    it('lists members other than the sharer and sends the ones chosen', async () => {
      shareService.createShare.mockResolvedValue(RESULT);
      radio('notifyMode', 'some').click();
      await fixture.whenStable();
      fixture.detectChanges();

      const rows = Array.from(el().querySelectorAll('ul[aria-label="Project members to notify"] li'));
      const lines = (r: Element) => Array.from(r.querySelectorAll('span span')).map(x => x.textContent?.trim()).join(' ');
      expect(rows.map(lines)).toEqual([
        'Olive Owner owner@x.edu', 'Ann Lee ann@x.edu', 'new@x.edu',
      ]);
      // Nobody chosen yet: "Choose people" with no one is a mistake, not "tell no one".
      expect((component as any).canSubmit()).toBe(false);

      rows[1].querySelector<HTMLInputElement>('input[type=checkbox]')!.click();
      fixture.detectChanges();
      expect(el().textContent).toContain('1 person selected');
      await (component as any).onShare();
      expect(shareService.createShare.mock.calls[0][3].notify).toEqual({ emails: ['ann@x.edu'] });
    });

    it('filters members by name or email', async () => {
      radio('notifyMode', 'some').click();
      await fixture.whenStable();
      (component as any).memberFilter.set('LEE');
      fixture.detectChanges();
      expect((component as any).filteredMembers().map((m: { email: string }) => m.email)).toEqual(['ann@x.edu']);
    });

    it('says so when the members cannot be loaded', async () => {
      projectApi.members.mockReturnValue(throwError(() => ({ status: 500 })));
      radio('notifyMode', 'some').click();
      await fixture.whenStable();
      fixture.detectChanges();
      expect(el().textContent).toContain('Couldn’t load the project’s members');
    });

    it('offers no notify section for a public link', () => {
      radio('accessLevel', 'public').click();
      fixture.detectChanges();
      expect(el().querySelector('input[name=notifyMode]')).toBeNull();
      expect(el().querySelector('#share-note')).toBeNull();
    });
  });
});

describe('ShareModalComponent in a CDK dialog', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ShareService, useValue: { listSharesForSession: vi.fn().mockResolvedValue({ shares: [] }) } },
        { provide: ProjectApiService, useValue: { members: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(data: ShareModalData) {
    return openInCdkDialog<ShareModalComponent, ShareModalData, boolean>(ShareModalComponent, { data });
  }

  it('names the dialog from its title and starts on the chosen access level', async () => {
    const { container } = await open({ sessionId: 'sess-1', ownerEmail: 'me@x.edu' });
    expectNamedDialog(container, { name: 'Share conversation' });
    const initial = container.querySelector<HTMLInputElement>('[cdkFocusInitial]');
    expect(initial?.name).toBe('accessLevel');
    expect(initial?.value).toBe('public');
  });

  it('starts on "Project members" for a task in a project', async () => {
    const { container } = await open({ sessionId: 'sess-1', ownerEmail: 'me@x.edu', projectId: 'prj_1' });
    expect(container.querySelectorAll('[cdkFocusInitial]').length).toBe(1);
    expect(container.querySelector<HTMLInputElement>('[cdkFocusInitial]')?.value).toBe('project');
  });
});
