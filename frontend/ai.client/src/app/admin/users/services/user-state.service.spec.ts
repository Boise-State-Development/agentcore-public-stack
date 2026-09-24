import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { UserStateService } from './user-state.service';
import { UserHttpService } from './user-http.service';
import { of } from 'rxjs';

describe('UserStateService', () => {
  let service: UserStateService;
  let httpService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    
    const httpServiceMock = {
      listUsers: vi.fn(),
      searchByEmail: vi.fn(),
      getUserDetail: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        UserStateService,
        { provide: UserHttpService, useValue: httpServiceMock },
      ],
    });

    service = TestBed.inject(UserStateService);
    httpService = TestBed.inject(UserHttpService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should load users', async () => {
    const mockResponse = {
      users: [{ userId: '1', email: 'test@example.com' }],
      nextCursor: 'cursor123',
    };
    httpService.listUsers.mockReturnValue(of(mockResponse));

    await service.loadUsers(true);

    expect(service.users()).toEqual(mockResponse.users);
    expect(service.nextCursor()).toBe('cursor123');
    expect(service.loading()).toBe(false);
  });

  it('should search by email', async () => {
    const mockResponse = {
      users: [{ userId: '1', email: 'test@example.com' }],
    };
    httpService.searchByEmail.mockReturnValue(of(mockResponse));

    await service.searchByEmail('test@example.com');

    expect(service.users()).toEqual(mockResponse.users);
    expect(service.searchQuery()).toBe('test@example.com');
    expect(service.loading()).toBe(false);
  });

  it('records how many profiles an email search matched', async () => {
    httpService.searchByEmail.mockReturnValue(
      of({ users: [{ userId: 'uuid-live' }, { userId: '100000001' }] }),
    );

    await service.searchByEmail('prof@example.com');

    expect(service.emailMatchCount()).toBe(2);
  });

  it('clears the email match count when the list reloads', async () => {
    httpService.searchByEmail.mockReturnValue(of({ users: [{ userId: 'a' }, { userId: 'b' }] }));
    httpService.listUsers.mockReturnValue(of({ users: [{ userId: 'a' }, { userId: 'c' }], nextCursor: null }));

    await service.searchByEmail('prof@example.com');
    await service.loadUsers(true);

    expect(service.emailMatchCount()).toBe(0);
  });

  it('should load user detail', async () => {
    const mockDetail = { userId: '1', email: 'test@example.com', profile: {} };
    httpService.getUserDetail.mockReturnValue(of(mockDetail));

    await service.loadUserDetail('1');

    expect(service.selectedUser()).toEqual(mockDetail);
    expect(service.loading()).toBe(false);
  });

  it('should set status filter and reload', async () => {
    const mockResponse = { users: [], nextCursor: null };
    httpService.listUsers.mockReturnValue(of(mockResponse));

    service.setStatusFilter('inactive');

    expect(service.statusFilter()).toBe('inactive');
  });

  it('should set domain filter and reload', async () => {
    const mockResponse = { users: [], nextCursor: null };
    httpService.listUsers.mockReturnValue(of(mockResponse));

    service.setDomainFilter('example.com');

    expect(service.domainFilter()).toBe('example.com');
  });

  it('should clear selection', () => {
    service.selectedUser.set({ userId: '1', email: 'test@example.com' } as any);

    service.clearSelection();

    expect(service.selectedUser()).toBeNull();
  });

  it('should reset state', () => {
    service.reset();

    expect(service.users()).toEqual([]);
    expect(service.selectedUser()).toBeNull();
    expect(service.searchQuery()).toBe('');
    expect(service.statusFilter()).toBe('active');
    expect(service.error()).toBeNull();
  });
});