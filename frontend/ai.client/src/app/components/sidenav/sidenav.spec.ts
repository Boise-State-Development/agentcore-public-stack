import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { signal } from '@angular/core';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';

describe('Sidenav', () => {
  let mockRouter: any;
  let mockSessionService: any;
  let mockBffSession: any;
  let mockSidenavService: any;
  let mockUserService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockRouter = { navigate: vi.fn() };
    mockSessionService = {
      currentSession: signal({ sessionId: 'test-session', userId: 'u1', title: 'Test Session', status: 'active' as const, createdAt: '', lastMessageAt: '', messageCount: 0 }),
      hasCurrentSession: signal(true),
    };
    // Phase 6c: logout is owned by the BFF SessionService now.
    mockBffSession = { logout: vi.fn().mockResolvedValue(undefined) };
    mockSidenavService = {
      isCollapsed: signal(false),
      close: vi.fn(),
      toggleCollapsed: vi.fn(),
    };
    mockUserService = { hasAnyRole: vi.fn().mockReturnValue(false) };

    TestBed.configureTestingModule({
      providers: [
        { provide: Router, useValue: mockRouter },
        { provide: SessionService, useValue: mockSessionService },
        { provide: BffSessionService, useValue: mockBffSession },
        { provide: SidenavService, useValue: mockSidenavService },
        { provide: UserService, useValue: mockUserService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { Sidenav } = await import('./sidenav');
    return TestBed.runInInjectionContext(() => new Sidenav());
  }

  it('should compute current session title', async () => {
    const component = await createComponent();
    expect(component.currentSessionTitle()).toBe('Test Session');

    mockSessionService.currentSession.set({ ...mockSessionService.currentSession(), title: '' });
    expect(component.currentSessionTitle()).toBe('Untitled Session');
  });

  it('should start new session and close sidenav', async () => {
    const component = await createComponent();
    component.newSession();
    expect(mockSidenavService.close).toHaveBeenCalled();
    expect(mockRouter.navigate).toHaveBeenCalledWith(['']);
  });

  it('should toggle sidenav collapse', async () => {
    const component = await createComponent();
    component.toggleCollapse();
    expect(mockSidenavService.toggleCollapsed).toHaveBeenCalled();
  });

  it('should handle logout via the BFF and route the user to /auth/login', async () => {
    const component = await createComponent();
    await component.handleLogout();
    expect(mockBffSession.logout).toHaveBeenCalledTimes(1);
    expect(mockRouter.navigate).toHaveBeenCalledWith(['/auth/login']);
  });
});
