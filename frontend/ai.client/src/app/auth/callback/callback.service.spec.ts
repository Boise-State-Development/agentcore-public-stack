import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { CallbackService } from './callback.service';
import { AuthService } from '../auth.service';
import { UserService } from '../user.service';
import { SessionService } from '../../session/services/session/session.service';

describe('CallbackService', () => {
  let service: CallbackService;
  let mockAuthService: any;
  let mockUserService: any;
  let mockSessionService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockAuthService = {
      handleCallback: vi.fn().mockResolvedValue(undefined),
    };

    mockUserService = {
      refreshUser: vi.fn(),
      ensurePermissionsLoaded: vi.fn().mockResolvedValue(undefined),
    };

    mockSessionService = {
      enableSessionsLoading: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        CallbackService,
        { provide: AuthService, useValue: mockAuthService },
        { provide: UserService, useValue: mockUserService },
        { provide: SessionService, useValue: mockSessionService },
      ],
    });

    service = TestBed.inject(CallbackService);
  });

  describe('exchangeCodeForTokens', () => {
    it('should delegate to AuthService.handleCallback and refresh user', async () => {
      await service.exchangeCodeForTokens('test-code', 'test-state');

      expect(mockAuthService.handleCallback).toHaveBeenCalledWith('test-code', 'test-state');
      expect(mockUserService.refreshUser).toHaveBeenCalled();
      expect(mockUserService.ensurePermissionsLoaded).toHaveBeenCalled();
      expect(mockSessionService.enableSessionsLoading).toHaveBeenCalled();
    });

    it('should propagate errors from AuthService.handleCallback', async () => {
      mockAuthService.handleCallback.mockRejectedValue(new Error('State mismatch'));

      await expect(service.exchangeCodeForTokens('code', 'bad-state'))
        .rejects.toThrow('State mismatch');
    });

    it('should propagate errors from permissions loading', async () => {
      mockUserService.ensurePermissionsLoaded.mockRejectedValue(new Error('Permissions failed'));

      await expect(service.exchangeCodeForTokens('code', 'state'))
        .rejects.toThrow('Permissions failed');
    });
  });
});
