import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Component, Input } from '@angular/core';
import { describe, it, expect, vi } from 'vitest';
import { ActivatedRoute, Router } from '@angular/router';
import { SharedViewPage } from './shared-view.page';
import { ShareService, SharedConversationResponse } from '../session/services/share/share.service';
import { SessionService } from '../session/services/session/session.service';
import { UserService } from '../auth/user.service';
import { MessageListComponent } from '../session/components/message-list/message-list.component';

// Stub MessageListComponent to avoid deep dependency chain (MarkdownService, etc.)
@Component({
  selector: 'app-message-list',
  template: '<div class="mock-message-list"></div>',
  standalone: true,
})
class MockMessageListComponent {
  @Input() messages: any[] = [];
  @Input() embeddedMode = false;
}

describe('SharedViewPage', () => {
  let component: SharedViewPage;
  let fixture: ComponentFixture<SharedViewPage>;
  let mockShareService: any;
  let mockSessionService: any;
  let mockUserService: any;
  let mockRouter: any;

  const mockConversation: SharedConversationResponse = {
    shareId: 'share-001',
    title: 'Test Shared Conversation',
    accessLevel: 'public',
    createdAt: '2025-06-01T00:00:00Z',
    ownerId: 'user-001',
    messages: [
      {
        id: 'msg-001',
        role: 'user',
        content: [{ type: 'text', text: 'Hello' }],
        createdAt: '2025-06-01T00:00:00Z',
      } as any,
      {
        id: 'msg-002',
        role: 'assistant',
        content: [{ type: 'text', text: 'Hi there' }],
        createdAt: '2025-06-01T00:00:01Z',
      } as any,
    ],
  };

  function createComponent(shareId: string | null = 'share-001') {
    mockShareService = {
      getSharedConversation: vi.fn(),
      exportSharedConversation: vi.fn(),
    };

    mockSessionService = {
      addSessionToCache: vi.fn(),
    };

    mockUserService = {
      currentUser: vi.fn().mockReturnValue({ user_id: 'user-002', email: 'viewer@example.com' }),
    };

    mockRouter = {
      navigate: vi.fn(),
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [SharedViewPage],
      providers: [
        { provide: ShareService, useValue: mockShareService },
        { provide: SessionService, useValue: mockSessionService },
        { provide: UserService, useValue: mockUserService },
        { provide: Router, useValue: mockRouter },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: {
              paramMap: {
                get: (key: string) => (key === 'shareId' ? shareId : null),
              },
            },
          },
        },
      ],
    });

    // Swap the real MessageListComponent for the mock to avoid MarkdownService
    TestBed.overrideComponent(SharedViewPage, {
      remove: { imports: [MessageListComponent] },
      add: { imports: [MockMessageListComponent] },
    });

    fixture = TestBed.createComponent(SharedViewPage);
    component = fixture.componentInstance;
  }

  // -----------------------------------------------------------------------
  // Basic rendering
  // -----------------------------------------------------------------------

  it('should create the component', () => {
    createComponent();
    expect(component).toBeTruthy();
  });

  it('should display conversation title on success', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Test Shared Conversation');
  });

  it('should display read-only snapshot banner', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Shared read-only snapshot');
  });

  it('should not display a message input field', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const textarea = el.querySelector('textarea');
    const messageInput = el.querySelector('app-message-input');
    expect(textarea).toBeNull();
    expect(messageInput).toBeNull();
  });

  // -----------------------------------------------------------------------
  // Error states
  // -----------------------------------------------------------------------

  it('should display access denied for 403 error', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockRejectedValue({ status: 403 });

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Access denied');
  });

  it('should display not found for 404 error', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockRejectedValue({ status: 404 });

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Conversation not found');
  });

  it('should display not found when shareId is missing from route', async () => {
    createComponent(null);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Conversation not found');
  });

  it('should display generic error for 500', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockRejectedValue({ status: 500 });

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Something went wrong');
  });

  // -----------------------------------------------------------------------
  // Export to new conversation
  // -----------------------------------------------------------------------

  it('should display export button when conversation is loaded', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    await component.ngOnInit();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Export to new conversation');
  });

  it('should not display export button when loading', () => {
    createComponent();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).not.toContain('Export to new conversation');
  });

  it('should call exportSharedConversation and navigate on export', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockResolvedValue({
      sessionId: 'new-sess-001',
      title: 'Test Shared Conversation (shared)',
    });

    await component.ngOnInit();
    fixture.detectChanges();

    await (component as any).onExport();
    fixture.detectChanges();

    expect(mockShareService.exportSharedConversation).toHaveBeenCalledWith('share-001');
    expect(mockSessionService.addSessionToCache).toHaveBeenCalledWith(
      'new-sess-001',
      'user-002',
      'Test Shared Conversation (shared)',
    );
    expect(mockRouter.navigate).toHaveBeenCalledWith(['/s', 'new-sess-001']);
  });

  it('should handle export failure gracefully', async () => {
    createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockRejectedValue(new Error('Export failed'));

    await component.ngOnInit();
    fixture.detectChanges();

    // Should not throw
    await (component as any).onExport();
    fixture.detectChanges();

    expect(mockRouter.navigate).not.toHaveBeenCalled();
  });
});
