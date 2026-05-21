import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { UserService } from './user.service';
import { SessionService } from './session.service';
import { ConfigService } from '../services/config.service';
import { BffSessionUser } from './bff-session.model';

describe('UserService', () => {
  let service: UserService;
  let userSignal: ReturnType<typeof signal<BffSessionUser | null>>;
  let isAuthenticatedSignal: ReturnType<typeof signal<boolean>>;
  let mockSessionService: {
    user: typeof userSignal;
    isAuthenticated: typeof isAuthenticatedSignal;
  };
  let mockConfigService: {
    appApiUrl: ReturnType<typeof vi.fn>;
  };

  const sessionUser: BffSessionUser = {
    user_id: 'user-123',
    email: 'test@example.com',
    name: 'Test User',
    roles: ['Admin'],
    picture: 'https://example.com/pic.jpg',
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    userSignal = signal<BffSessionUser | null>(null);
    isAuthenticatedSignal = signal(false);
    mockSessionService = {
      user: userSignal,
      isAuthenticated: isAuthenticatedSignal,
    };
    mockConfigService = {
      appApiUrl: vi.fn().mockReturnValue('http://localhost:8000'),
    };

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        UserService,
        { provide: SessionService, useValue: mockSessionService },
        { provide: ConfigService, useValue: mockConfigService },
      ]
    });

    service = TestBed.inject(UserService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
  });

  describe('getUser', () => {
    it('should return null when no BFF session', () => {
      expect(service.getUser()).toBeNull();
    });

    it('should map BFF session payload onto the legacy User shape', () => {
      userSignal.set(sessionUser);

      const user = service.getUser();
      expect(user).toEqual({
        email: 'test@example.com',
        user_id: 'user-123',
        firstName: 'Test',
        lastName: 'User',
        fullName: 'Test User',
        roles: ['Admin'],
        picture: 'https://example.com/pic.jpg',
      });
    });

    it('should fall back to email when name is empty', () => {
      userSignal.set({ ...sessionUser, name: null });

      const user = service.getUser();
      expect(user?.fullName).toBe('test@example.com');
      expect(user?.firstName).toBe('test@example.com');
      expect(user?.lastName).toBe('');
    });
  });

  describe('hasRole', () => {
    it('returns false when no user', () => {
      expect(service.hasRole('Admin')).toBe(false);
    });

    it('returns true when user has role', () => {
      userSignal.set(sessionUser);
      expect(service.hasRole('Admin')).toBe(true);
    });

    it('returns false when user does not have role', () => {
      userSignal.set(sessionUser);
      expect(service.hasRole('User')).toBe(false);
    });
  });

  describe('hasAnyRole', () => {
    it('returns false when no user', () => {
      expect(service.hasAnyRole(['Admin', 'User'])).toBe(false);
    });

    it('returns true when user has at least one role', () => {
      userSignal.set(sessionUser);
      expect(service.hasAnyRole(['Admin', 'User'])).toBe(true);
    });

    it('returns false when user has no matching roles', () => {
      userSignal.set(sessionUser);
      expect(service.hasAnyRole(['User', 'Guest'])).toBe(false);
    });
  });
});
