import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { ShareModalComponent, ShareModalData } from './share-modal.component';
import { ShareService, ShareResponse, ShareListResponse } from '../../services/share/share.service';

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
});
