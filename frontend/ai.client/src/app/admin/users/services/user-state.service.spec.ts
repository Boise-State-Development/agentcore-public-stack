import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { UserStateService } from './user-state.service';
import { UserHttpService } from './user-http.service';
import { Subject, of, throwError } from 'rxjs';
import { UserListResponse } from '../models';

describe('UserStateService', () => {
  let service: UserStateService;
  let httpService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();

    const httpServiceMock = {
      listUsers: vi.fn(),
      searchUsers: vi.fn(),
      listDomains: vi.fn(),
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
      totalCount: 40,
    };
    httpService.listUsers.mockReturnValue(of(mockResponse));

    await service.loadUsers(true);

    expect(service.users()).toEqual(mockResponse.users);
    expect(service.nextCursor()).toBe('cursor123');
    expect(service.totalCount()).toBe(40);
    expect(service.loading()).toBe(false);
  });

  it('lists every status by default', async () => {
    httpService.listUsers.mockReturnValue(of({ users: [] }));

    await service.loadUsers(true);

    expect(httpService.listUsers).toHaveBeenCalledWith(expect.objectContaining({ status: 'all' }));
  });

  it('searches with the query and the current filters', async () => {
    const mockResponse = { users: [{ userId: '1', email: 'oscarfilson@example.com' }], totalCount: 1 };
    httpService.searchUsers.mockReturnValue(of(mockResponse));
    service.statusFilter.set('suspended');
    service.domainFilter.set('example.com');

    await service.search('  oscarfilson ');

    expect(httpService.searchUsers).toHaveBeenCalledWith({
      q: 'oscarfilson',
      status: 'suspended',
      domain: 'example.com',
      limit: 25,
      cursor: undefined,
    });
    expect(httpService.listUsers).not.toHaveBeenCalled();
    expect(service.users()).toEqual(mockResponse.users);
    expect(service.searchQuery()).toBe('oscarfilson');
    expect(service.showingResults()).toBe(true);
    expect(service.loading()).toBe(false);
  });

  it('pages through search results with the cursor', async () => {
    httpService.searchUsers
      .mockReturnValueOnce(of({ users: [{ userId: 'a', email: 'a@x.com' }], nextCursor: 'next' }))
      .mockReturnValueOnce(of({ users: [{ userId: 'b', email: 'b@x.com' }] }));

    await service.search('x');
    expect(service.hasMore()).toBe(true);
    await service.loadUsers(false);

    expect(httpService.searchUsers).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'x', cursor: 'next' }));
    expect(service.users().map((u) => u.userId)).toEqual(['a', 'b']);
    expect(service.hasMore()).toBe(false);
  });

  it('keeps searching when a filter changes', async () => {
    httpService.searchUsers.mockReturnValue(of({ users: [] }));
    await service.search('oscar');

    service.setStatusFilter('inactive');
    await Promise.resolve();

    expect(httpService.searchUsers).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: 'oscar', status: 'inactive' }),
    );
    expect(httpService.listUsers).not.toHaveBeenCalled();
  });

  it('a blank search goes back to the plain list', async () => {
    httpService.listUsers.mockReturnValue(of({ users: [] }));

    await service.search('   ');

    expect(httpService.searchUsers).not.toHaveBeenCalled();
    expect(httpService.listUsers).toHaveBeenCalled();
    expect(service.showingResults()).toBe(false);
  });

  it('ignores a slower response to an earlier search', async () => {
    const slow = new Subject<UserListResponse>();
    httpService.searchUsers
      .mockReturnValueOnce(slow)
      .mockReturnValueOnce(of({ users: [{ userId: 'new', email: 'new@x.com' }] }));

    const first = service.search('osc');
    await service.search('oscar');
    slow.next({ users: [{ userId: 'old', email: 'old@x.com' }] } as UserListResponse);
    slow.complete();
    await first;

    expect(service.users().map((u) => u.userId)).toEqual(['new']);
    expect(service.resultQuery()).toBe('oscar');
    expect(service.loading()).toBe(false);
  });

  it('shows a generic message, not the raw error, when a search fails', async () => {
    httpService.searchUsers.mockReturnValue(
      throwError(() => new Error('Http failure response for https://internal/admin/users/search: 500')),
    );

    await service.search('oscar');

    expect(service.error()).toBe('Search failed. Please try again.');
    expect(service.users()).toEqual([]);
    expect(service.loading()).toBe(false);
  });

  it('counts the profiles that share a searched email', async () => {
    httpService.searchUsers.mockReturnValue(
      of({
        users: [
          { userId: 'uuid-live', email: 'prof@example.com' },
          { userId: '100000001', email: 'prof@example.com' },
          { userId: 'other', email: 'prof@example.com.au' },
        ],
      }),
    );

    await service.search('Prof@Example.com');

    expect(service.exactEmail()).toBe('prof@example.com');
    expect(service.emailMatchCount()).toBe(2);
  });

  it('has no email match count for a partial search or the plain list', async () => {
    httpService.searchUsers.mockReturnValue(
      of({ users: [{ userId: 'a', email: 'prof@example.com' }, { userId: 'b', email: 'prof@example.com' }] }),
    );
    httpService.listUsers.mockReturnValue(of({ users: [{ userId: 'a', email: 'prof@example.com' }] }));

    await service.search('prof');
    expect(service.emailMatchCount()).toBe(0);

    await service.search('prof@example.com');
    expect(service.emailMatchCount()).toBe(2);

    await service.clearSearch();
    await Promise.resolve();
    expect(service.emailMatchCount()).toBe(0);
  });

  it('loads domains, and leaves them empty on failure', async () => {
    httpService.listDomains.mockReturnValue(of(['example.com', 'u.example.com']));
    await service.loadDomains();
    expect(service.domains()).toEqual(['example.com', 'u.example.com']);

    httpService.listDomains.mockReturnValue(throwError(() => new Error('boom')));
    await service.loadDomains();
    expect(service.domains()).toEqual([]);
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
    expect(httpService.listUsers).toHaveBeenCalledWith(expect.objectContaining({ status: 'inactive' }));
  });

  it('should set domain filter and reload', async () => {
    const mockResponse = { users: [], nextCursor: null };
    httpService.listUsers.mockReturnValue(of(mockResponse));

    service.setDomainFilter('example.com');

    expect(service.domainFilter()).toBe('example.com');
    expect(httpService.listUsers).toHaveBeenCalledWith(expect.objectContaining({ domain: 'example.com' }));
  });

  it('should clear selection', () => {
    service.selectedUser.set({ userId: '1', email: 'test@example.com' } as any);

    service.clearSelection();

    expect(service.selectedUser()).toBeNull();
  });

  it('should reset state', () => {
    service.searchQuery.set('oscar');
    service.statusFilter.set('suspended');

    service.reset();

    expect(service.users()).toEqual([]);
    expect(service.selectedUser()).toBeNull();
    expect(service.searchQuery()).toBe('');
    expect(service.resultQuery()).toBe('');
    expect(service.statusFilter()).toBe('all');
    expect(service.totalCount()).toBeNull();
    expect(service.error()).toBeNull();
  });
});
