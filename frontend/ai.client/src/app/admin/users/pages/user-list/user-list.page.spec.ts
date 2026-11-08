import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { SEARCH_DEBOUNCE_MS, UserListPage } from './user-list.page';
import { UserHttpService } from '../../services/user-http.service';
import { UserStateService } from '../../services/user-state.service';
import { UserListItem, UserListResponse } from '../../models';

function user(
  userId: string,
  email: string,
  status: UserListItem['status'] = 'active',
): UserListItem {
  return { userId, email, name: 'Oscar Filson', status, lastLoginAt: '2026-09-01T00:00:00Z' };
}

describe('UserListPage', () => {
  let fixture: ComponentFixture<UserListPage>;
  let http: {
    listUsers: ReturnType<typeof vi.fn>;
    searchUsers: ReturnType<typeof vi.fn>;
    listDomains: ReturnType<typeof vi.fn>;
    getUserDetail: ReturnType<typeof vi.fn>;
  };

  const el = () => fixture.nativeElement as HTMLElement;
  const input = () => el().querySelector('#user-search') as HTMLInputElement;

  /** Flush promises and zero-delay timers (change detection), but not the search debounce. */
  async function settle(): Promise<void> {
    await vi.advanceTimersByTimeAsync(1);
    fixture.detectChanges();
  }

  function type(text: string): void {
    input().value = text;
    input().dispatchEvent(new Event('input'));
  }

  beforeEach(async () => {
    vi.useFakeTimers();
    http = {
      listUsers: vi
        .fn()
        .mockReturnValue(of({ users: [user('u1', 'a@example.com')], totalCount: 1 })),
      searchUsers: vi.fn().mockReturnValue(of({ users: [], totalCount: 0 } as UserListResponse)),
      listDomains: vi.fn().mockReturnValue(of([])),
      getUserDetail: vi.fn(),
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [UserListPage],
      providers: [provideRouter([]), { provide: UserHttpService, useValue: http }],
    });
    TestBed.inject(UserStateService).reset();

    fixture = TestBed.createComponent(UserListPage);
    fixture.detectChanges();
    await settle();
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('is a search box for names and emails, not an email field', () => {
    expect(input().type).toBe('search');
    expect(input().placeholder).toBe('Search by name or email...');
  });

  it('searches as you type, once, after a pause', async () => {
    type('o');
    type('osc');
    type('oscar');
    expect(http.searchUsers).not.toHaveBeenCalled();

    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await settle();

    expect(http.searchUsers).toHaveBeenCalledTimes(1);
    expect(http.searchUsers).toHaveBeenCalledWith(
      expect.objectContaining({ q: 'oscar', status: 'all' }),
    );
  });

  it('searches at once on Enter, without a second request after the pause', async () => {
    type('oscar');
    input().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await settle();
    expect(http.searchUsers).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await settle();
    expect(http.searchUsers).toHaveBeenCalledTimes(1);
  });

  it('says when nothing matches', async () => {
    type('zzzz');
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await settle();

    expect(el().textContent).toContain('No users match “zzzz”');
  });

  it('labels every row with its status', async () => {
    http.searchUsers.mockReturnValue(
      of({
        users: [
          user('u1', 'oscarfilson@example.com', 'active'),
          user('u2', 'oscarfilson@u.example.com', 'suspended'),
          user('u3', 'oscar@example.com', 'inactive'),
        ],
        totalCount: 3,
      }),
    );
    type('oscar');
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await settle();

    const text = el().textContent ?? '';
    expect(text).toContain('Active');
    expect(text).toContain('Suspended');
    expect(text).toContain('Inactive');
    expect(text).toContain('3 matches');
  });

  it('flags duplicate profiles only for rows with the searched email', async () => {
    http.searchUsers.mockReturnValue(
      of({
        users: [
          user('uuid-live', 'prof@example.com'),
          user('100000001', 'prof@example.com'),
          user('other', 'prof@example.com.au'),
        ],
      }),
    );
    type('prof@example.com');
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await settle();

    const text = el().textContent ?? '';
    expect(text).toContain('2 profiles share this email');
    expect(text.match(/Signs in/g)?.length).toBe(1);
    expect(text.match(/Duplicate/g)?.length).toBe(1);
  });

  it('shows a spinner in the search box while a search loads', async () => {
    const state = TestBed.inject(UserStateService);
    state.searchQuery.set('oscar');
    state.loading.set(true);
    fixture.detectChanges();

    expect(el().querySelector('[aria-label="Searching"]')).not.toBeNull();
  });

  it('offers a domain filter when there is more than one domain', async () => {
    http.listDomains.mockReturnValue(of(['example.com', 'u.example.com']));
    fixture = TestBed.createComponent(UserListPage);
    fixture.detectChanges();
    await settle();

    const select = el().querySelector('#user-domain-filter') as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent?.trim())).toEqual([
      'All domains',
      'example.com',
      'u.example.com',
    ]);

    select.value = select.options[2].value;
    select.dispatchEvent(new Event('change'));
    await settle();
    expect(http.listUsers).toHaveBeenLastCalledWith(
      expect.objectContaining({ domain: 'u.example.com' }),
    );
  });

  it('clears the search back to the plain list', async () => {
    type('oscar');
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await settle();
    http.listUsers.mockClear();

    (el().querySelector('[aria-label="Clear search"]') as HTMLButtonElement).click();
    await settle();

    expect(input().value).toBe('');
    expect(http.listUsers).toHaveBeenCalledTimes(1);
  });
});
