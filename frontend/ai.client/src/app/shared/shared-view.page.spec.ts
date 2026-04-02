import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, vi } from 'vitest';
import { ActivatedRoute, Router } from '@angular/router';
import { Component, input } from '@angular/core';
import { ShareService, SharedConversationResponse } from '../session/services/share/share.service';
import { SessionService } from '../session/services/session/session.service';
import { UserService } from '../auth/user.service';

// Create a mock MessageListComponent to avoid external template resolution
@Component({
  selector: 'app-message-list',
  template: '<div class="mock-message-list"></div>',
  standalone: true,
})
class MockMessageListComponent {
  messages = input.required<any[]>();
  embeddedMode = input<boolean>(false);
}

// Dynamically create the component under test with the mock dependency
@Component({
  selector: 'app-shared-view-test',
  template: '<div></div>',
  standalone: true,
})
class TestSharedViewPage {
  // We'll test the actual SharedViewPage logic by importing it dynamically
}

describe('SharedViewPage', () => {
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

  function setupMocks(shareId: string | null = 'share-001') {
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

    return {
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
    };
  }

  // Import SharedViewPage dynamically and override its imports
  async function createComponent(shareId: string | null = 'share-001') {
    const { providers } = setupMocks(shareId);

    // Dynamically import to avoid template resolution at module load time
    const { SharedViewPage } = await import('./shared-view.page');

    TestBed.resetTestingModule();

    // Override the component to use mock MessageListComponent
    TestBed.overrideComponent(SharedViewPage, {
      set: {
        imports: [MockMessageListComponent],
        template: '<div></div>',
      },
    });

    await TestBed.configureTestingModule({
      imports: [SharedViewPage],
      providers,
    }).compileComponents();

    const fixture = TestBed.createComponent(SharedViewPage);
    const component = fixture.componentInstance;

    return { fixture, component };
  }

  // -----------------------------------------------------------------------
  // Basic component creation and lifecycle
  // -----------------------------------------------------------------------

  it('should create the component', async () => {
    const { component } = await createComponent();
    expect(component).toBeTruthy();
  });

  it('should initialize with loading state', async () => {
    const { component } = await createComponent();
    expect((component as any).isLoading()).toBe(true);
    expect((component as any).conversation()).toBeNull();
    expect((component as any).errorStatus()).toBeNull();
  });

  // -----------------------------------------------------------------------
  // ngOnInit - successful load
  // -----------------------------------------------------------------------

  it('should load conversation on init', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    await component.ngOnInit();

    expect(mockShareService.getSharedConversation).toHaveBeenCalledWith('share-001');
    expect((component as any).conversation()).toEqual(mockConversation);
    expect((component as any).messages().length).toBe(2);
    expect((component as any).isLoading()).toBe(false);
  });

  it('should set conversation title from response', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    await component.ngOnInit();

    expect((component as any).conversation()!.title).toBe('Test Shared Conversation');
  });

  // -----------------------------------------------------------------------
  // ngOnInit - error states
  // -----------------------------------------------------------------------

  it('should set 403 error status on access denied', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockRejectedValue({ status: 403 });

    await component.ngOnInit();

    expect((component as any).errorStatus()).toBe(403);
    expect((component as any).isLoading()).toBe(false);
  });

  it('should set 404 error status on not found', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockRejectedValue({ status: 404 });

    await component.ngOnInit();

    expect((component as any).errorStatus()).toBe(404);
    expect((component as any).isLoading()).toBe(false);
  });

  it('should set 404 error when shareId is missing from route', async () => {
    const { component } = await createComponent(null);

    await component.ngOnInit();

    expect((component as any).errorStatus()).toBe(404);
    expect((component as any).isLoading()).toBe(false);
    expect(mockShareService.getSharedConversation).not.toHaveBeenCalled();
  });

  it('should set 500 error status on server error', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockRejectedValue({ status: 500 });

    await component.ngOnInit();

    expect((component as any).errorStatus()).toBe(500);
    expect((component as any).isLoading()).toBe(false);
  });

  it('should default to 500 error when status is not provided', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockRejectedValue(new Error('Network error'));

    await component.ngOnInit();

    expect((component as any).errorStatus()).toBe(500);
  });

  // -----------------------------------------------------------------------
  // Export functionality
  // -----------------------------------------------------------------------

  it('should call exportSharedConversation on export', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockResolvedValue({
      sessionId: 'new-sess-001',
      title: 'Test Shared Conversation (shared)',
    });

    await component.ngOnInit();
    await (component as any).onExport();

    expect(mockShareService.exportSharedConversation).toHaveBeenCalledWith('share-001');
  });

  it('should add session to cache after export', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockResolvedValue({
      sessionId: 'new-sess-001',
      title: 'Test Shared Conversation (shared)',
    });

    await component.ngOnInit();
    await (component as any).onExport();

    expect(mockSessionService.addSessionToCache).toHaveBeenCalledWith(
      'new-sess-001',
      'user-002',
      'Test Shared Conversation (shared)',
    );
  });

  it('should navigate to new session after export', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockResolvedValue({
      sessionId: 'new-sess-001',
      title: 'Test Shared Conversation (shared)',
    });

    await component.ngOnInit();
    await (component as any).onExport();

    expect(mockRouter.navigate).toHaveBeenCalledWith(['/s', 'new-sess-001']);
  });

  it('should set isExporting during export', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);

    let resolveExport: (value: any) => void;
    mockShareService.exportSharedConversation.mockReturnValue(
      new Promise((resolve) => {
        resolveExport = resolve;
      }),
    );

    await component.ngOnInit();
    const exportPromise = (component as any).onExport();

    expect((component as any).isExporting()).toBe(true);

    resolveExport!({ sessionId: 'new-sess-001', title: 'Test' });
    await exportPromise;

    expect((component as any).isExporting()).toBe(false);
  });

  it('should handle export failure gracefully', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockRejectedValue(new Error('Export failed'));

    await component.ngOnInit();

    // Should not throw
    await (component as any).onExport();

    expect(mockRouter.navigate).not.toHaveBeenCalled();
    expect((component as any).isExporting()).toBe(false);
  });

  it('should not export when conversation is null', async () => {
    const { component } = await createComponent();
    // Don't load conversation

    await (component as any).onExport();

    expect(mockShareService.exportSharedConversation).not.toHaveBeenCalled();
  });

  it('should use anonymous userId when user is not logged in', async () => {
    const { component } = await createComponent();
    mockShareService.getSharedConversation.mockResolvedValue(mockConversation);
    mockShareService.exportSharedConversation.mockResolvedValue({
      sessionId: 'new-sess-001',
      title: 'Test',
    });
    mockUserService.currentUser.mockReturnValue(null);

    await component.ngOnInit();
    await (component as any).onExport();

    expect(mockSessionService.addSessionToCache).toHaveBeenCalledWith(
      'new-sess-001',
      'anonymous',
      'Test',
    );
  });
});
