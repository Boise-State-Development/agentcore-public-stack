// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { SessionService } from '../../../../session/services/session/session.service';
import { ChatStateService } from '../../../../session/services/chat/chat-state.service';
import { SidenavService } from '../../../../services/sidenav/sidenav.service';
import { ToastService } from '../../../../services/toast/toast.service';
import { UserService } from '../../../../auth/user.service';
import { ProjectsService } from '../../../../projects/services/projects.service';
import { FEATURES } from '../../../../services/features';
import { SESSION_FILTER_STORAGE_KEY } from './session-title-filter';
import { SessionList } from './session-list';

/**
 * The filter as rendered: the real template, driven by real input and keydown
 * events, so the date grouping, the count, the "No matching" state and Escape
 * are checked where the user meets them rather than only on the signals.
 */
describe('SessionList title filter (rendered)', () => {
  const today = new Date().toISOString();
  const row = (sessionId: string, title: string) => ({
    sessionId,
    userId: 'user-1',
    title,
    status: 'active' as const,
    createdAt: today,
    lastMessageAt: today,
    messageCount: 2,
  });
  const sessions = [
    row('a', 'BIO 101 syllabus rewrite'),
    row('b', 'Window functions in Postgres'),
    row('c', 'bio lab safety quiz'),
  ];

  let merged: ReturnType<typeof signal<{ sessions: typeof sessions; nextToken: string | null }>>;
  let fixture: ComponentFixture<SessionList>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    sessionStorage.removeItem(SESSION_FILTER_STORAGE_KEY);
    merged = signal({ sessions, nextToken: null as string | null });
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        {
          provide: SessionService,
          useValue: {
            mergedSessionsResource: merged,
            currentSession: signal(sessions[0]),
            sessionsResource: {
              value: () => merged(),
              error: () => null,
              isLoading: signal(false),
              reload: vi.fn(),
            },
            isLoadingMoreSessions: signal(false),
            loadMoreSessionsError: signal(false),
            loadMoreSessions: vi.fn().mockResolvedValue(undefined),
            isLocallyRead: () => false,
          },
        },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false, isSessionUnread: () => false } },
        { provide: SidenavService, useValue: { close: vi.fn() } },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
        { provide: Dialog, useValue: { open: vi.fn() } },
        { provide: UserService, useValue: { currentUser: signal(null) } },
        {
          provide: ProjectsService,
          useValue: { projects$: signal([]), available$: signal(null), loading$: signal(false), load: vi.fn() },
        },
        { provide: FEATURES, useValue: { projects: false, conversationSearch: false } },
      ],
    });
  });

  afterEach(() => {
    sessionStorage.removeItem(SESSION_FILTER_STORAGE_KEY);
    TestBed.resetTestingModule();
  });

  async function render(): Promise<HTMLElement> {
    fixture = TestBed.createComponent(SessionList);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  function input(root: HTMLElement): HTMLInputElement {
    return root.querySelector('input[aria-label="Search conversations by title"]') as HTMLInputElement;
  }

  async function type(root: HTMLElement, value: string): Promise<void> {
    const el = input(root);
    el.value = value;
    el.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    await fixture.whenStable();
  }

  const titles = (root: HTMLElement) =>
    Array.from(root.querySelectorAll('a[href^="/s/"] span.min-w-0')).map(el => el.textContent?.trim());
  const groupHeadings = (root: HTMLElement) => Array.from(root.querySelectorAll('h3')).map(el => el.textContent?.trim());
  const status = (root: HTMLElement) => root.querySelector('p[role="status"]')?.textContent?.trim();

  it('shows the date grouping with no query', async () => {
    const root = await render();
    expect(groupHeadings(root)).toEqual(['Today']);
    expect(titles(root)).toHaveLength(3);
    expect(status(root)).toBeUndefined();
  });

  it('replaces the grouping with a flat, counted list of matches while typing', async () => {
    const root = await render();
    await type(root, 'bIo');

    expect(groupHeadings(root)).toEqual([]);
    expect(titles(root)).toEqual(['BIO 101 syllabus rewrite', 'bio lab safety quiz']);
    expect(status(root)).toBe('2 matches');
  });

  it('says "No matching conversations", not the first-run empty state', async () => {
    const root = await render();
    await type(root, 'kubernetes');

    expect(root.textContent).toContain('No matching conversations');
    expect(root.textContent).not.toContain('No Chats Yet');
    expect(status(root)).toBe('0 matches');
  });

  it('notes that only loaded conversations are searched while more pages exist', async () => {
    merged.set({ sessions, nextToken: 'p2' });
    const root = await render();
    await type(root, 'bio');
    expect(root.textContent).toContain('Searching loaded conversations');

    merged.set({ sessions, nextToken: null });
    fixture.detectChanges();
    expect(root.textContent).not.toContain('Searching loaded conversations');
  });

  it('Escape in the box clears the query and brings the grouping back', async () => {
    const root = await render();
    await type(root, 'bio');

    input(root).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    fixture.detectChanges();
    await fixture.whenStable();

    expect(input(root).value).toBe('');
    expect(groupHeadings(root)).toEqual(['Today']);
    expect(titles(root)).toHaveLength(3);
  });

  it('the clear button empties the box and returns focus to it', async () => {
    const root = await render();
    await type(root, 'bio');

    (root.querySelector('button[aria-label="Clear search"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    await fixture.whenStable();

    expect(input(root).value).toBe('');
    expect(document.activeElement).toBe(input(root));
    expect(root.querySelector('button[aria-label="Clear search"]')).toBeNull();
  });

  it('shows a query restored from the last visit in the box', async () => {
    sessionStorage.setItem(SESSION_FILTER_STORAGE_KEY, 'window');
    const root = await render();
    expect(input(root).value).toBe('window');
    expect(titles(root)).toEqual(['Window functions in Postgres']);
  });

  it('is not drawn with conversation search on, and a stored query does not narrow the list', async () => {
    TestBed.overrideProvider(FEATURES, { useValue: { projects: false, conversationSearch: true } });
    sessionStorage.setItem(SESSION_FILTER_STORAGE_KEY, 'window');
    const root = await render();

    expect(input(root)).toBeNull();
    expect(root.querySelector('[role="search"]')).toBeNull();
    expect(groupHeadings(root)).toEqual(['Today']);
    expect(titles(root)).toHaveLength(3);
  });
});
