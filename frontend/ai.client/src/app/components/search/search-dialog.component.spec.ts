// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DIALOG_DATA, Dialog, DialogRef } from '@angular/cdk/dialog';
import { Router, provideRouter } from '@angular/router';
import { FEATURES } from '../../services/features';
import { SessionService } from '../../session/services/session/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ConversationSearchResponse } from './conversation-search-api.service';
import {
  LEXICAL_DEBOUNCE_MS,
  SEARCH_DIALOG_STORAGE_KEY,
  SearchDialogComponent,
  TEXT_SEARCH_PAUSE_MS,
} from './search-dialog.component';
import { SearchDialogService } from './search-dialog.service';

const iso = new Date().toISOString();
const session = (sessionId: string, title: string, extra: Record<string, unknown> = {}) => ({
  sessionId,
  userId: 'u1',
  title,
  status: 'active' as const,
  createdAt: iso,
  lastMessageAt: iso,
  messageCount: 2,
  ...extra,
});

describe('SearchDialogComponent', () => {
  let fixture: ComponentFixture<SearchDialogComponent>;
  let http: HttpTestingController;
  let dialogRef: { close: ReturnType<typeof vi.fn> };
  let navigate: ReturnType<typeof vi.spyOn>;
  let sessions: ReturnType<typeof session>[];

  function configure(data: { query?: string } | null = null, features = { projects: true, conversationSearch: true }) {
    dialogRef = { close: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: DialogRef, useValue: dialogRef },
        { provide: DIALOG_DATA, useValue: data },
        { provide: Dialog, useValue: { open: vi.fn() } },
        { provide: FEATURES, useValue: features },
        { provide: SidenavService, useValue: { close: vi.fn() } },
        { provide: SearchDialogService, useValue: { refocus: signal(null) } },
        {
          provide: SessionService,
          useValue: {
            mergedSessionsResource: signal({ sessions, nextToken: null }),
            currentSession: signal(null),
          },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  }

  function render(): HTMLElement {
    fixture = TestBed.createComponent(SearchDialogComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const input = (root: HTMLElement) => root.querySelector('input[role="combobox"]') as HTMLInputElement;
  const options = (root: HTMLElement) => Array.from(root.querySelectorAll('[role="option"]')) as HTMLElement[];
  const optionTitles = (root: HTMLElement) =>
    options(root).map(o => (o.querySelector('span') as HTMLElement).textContent?.replace(/\s+/g, ' ').trim());
  const headings = (root: HTMLElement) =>
    Array.from(root.querySelectorAll('[role="group"] > [role="presentation"]')).map(h => h.textContent?.trim());

  function type(root: HTMLElement, value: string): void {
    input(root).value = value;
    input(root).dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function key(root: HTMLElement, k: string): KeyboardEvent {
    const event = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
    input(root).dispatchEvent(event);
    fixture.detectChanges();
    return event;
  }

  function respond(mode: 'lexical' | 'all', body: ConversationSearchResponse): void {
    const req = http.expectOne(r => r.url.endsWith('/sessions/search') && r.params.get('mode') === mode);
    req.flush(body);
    fixture.detectChanges();
  }

  beforeEach(() => {
    vi.useFakeTimers();
    sessionStorage.removeItem(SEARCH_DIALOG_STORAGE_KEY);
    sessions = Array.from({ length: 10 }, (_, i) => session(`s${i}`, `Chat number ${i}`));
    sessions[1] = session('s1', 'Budget review', { preferences: { assistantId: 'ast-9' } });
  });

  afterEach(() => {
    http.verify();
    vi.useRealTimers();
    TestBed.resetTestingModule();
    sessionStorage.removeItem(SEARCH_DIALOG_STORAGE_KEY);
  });

  it('empty state: the eight most recent conversations and the actions, with no request', () => {
    configure();
    const root = render();
    expect(headings(root)).toEqual(['Recent', 'Actions']);
    expect(optionTitles(root).slice(0, 8)).toEqual(sessions.slice(0, 8).map(s => s.title));
    expect(root.textContent).toContain('New conversation');
    expect(root.textContent).toContain('New project');
    expect(root.textContent).toContain('New agent');
    vi.advanceTimersByTime(2000);
    http.expectNone(() => true);
  });

  it('hides New project when Projects is off', () => {
    configure(null, { projects: false, conversationSearch: true });
    const root = render();
    expect(root.textContent).not.toContain('New project');
  });

  it('filters loaded titles at once, then title matches, then full text after a pause', () => {
    configure();
    const root = render();
    type(root, 'budget');

    expect(headings(root)).toEqual(['Conversations']);
    expect(optionTitles(root)).toEqual(['Budget review']);
    http.expectNone(() => true);

    vi.advanceTimersByTime(LEXICAL_DEBOUNCE_MS);
    respond('lexical', {
      results: [
        { sessionId: 'old', title: 'Old budget', lastMessageAt: iso, archived: true, alsoMatched: [], matchKind: 'title', snippet: '' },
      ],
      textSearchAvailable: true,
    });
    // Server rows lead; a loaded title match the server lacked still shows, once.
    expect(optionTitles(root)).toEqual(['Old budget', 'Budget review']);
    expect(root.textContent).toContain('Archived');

    vi.advanceTimersByTime(TEXT_SEARCH_PAUSE_MS - LEXICAL_DEBOUNCE_MS);
    respond('all', {
      results: [
        {
          sessionId: 's5',
          title: 'Chat number 5',
          lastMessageAt: iso,
          archived: false,
          messageId: 'msg-s5-4',
          alsoMatched: ['msg-s5-8'],
          matchKind: 'text',
          snippet: 'the budget totals',
          score: 0.9,
        },
      ],
      textSearchAvailable: true,
    });
    expect(optionTitles(root)).toEqual(['Chat number 5', 'Budget review']);
    expect(root.textContent).toContain('the budget totals');
    expect(root.querySelector('mark')?.textContent).toBe('budget');
    expect(root.textContent).toContain('2 matches in this conversation');
  });

  it('a short query never reaches the full-text search', () => {
    configure();
    const root = render();
    type(root, 'bu');
    vi.advanceTimersByTime(LEXICAL_DEBOUNCE_MS);
    respond('lexical', { results: [], textSearchAvailable: true });
    vi.advanceTimersByTime(5000);
    http.expectNone(r => r.params.get('mode') === 'all');
  });

  it('Enter with nothing highlighted searches the full text at once', () => {
    configure();
    const root = render();
    type(root, 'window function');
    const event = key(root, 'Enter');
    expect(event.defaultPrevented).toBe(true);
    vi.advanceTimersByTime(0);
    respond('all', { results: [], textSearchAvailable: true });
    vi.advanceTimersByTime(5000);
    http.expectNone(() => true);
  });

  it('↑/↓ move the highlight across sections and ↵ opens it at the matching turn', () => {
    configure();
    const root = render();
    type(root, 'budget');
    vi.advanceTimersByTime(LEXICAL_DEBOUNCE_MS);
    respond('lexical', { results: [], textSearchAvailable: true });
    vi.advanceTimersByTime(TEXT_SEARCH_PAUSE_MS);
    respond('all', {
      results: [
        { sessionId: 's1', title: 'Budget review', lastMessageAt: iso, archived: false, assistantId: 'ast-9', messageId: 'msg-s1-6', alsoMatched: [], matchKind: 'title', snippet: 'x' },
      ],
      textSearchAvailable: true,
    });

    expect(input(root).getAttribute('aria-activedescendant')).toBeNull();
    key(root, 'ArrowDown');
    const first = options(root)[0];
    expect(input(root).getAttribute('aria-activedescendant')).toBe(first.id);
    expect(first.getAttribute('aria-selected')).toBe('true');
    key(root, 'ArrowUp');
    expect(input(root).getAttribute('aria-activedescendant')).toBe(options(root).at(-1)!.id);
    key(root, 'ArrowDown');

    key(root, 'Enter');
    expect(dialogRef.close).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith(['/s', 's1'], { queryParams: { assistantId: 'ast-9', m: 'msg-s1-6' } });
  });

  it('an action opens its page', () => {
    configure();
    const root = render();
    const newConversation = options(root).find(o => o.textContent?.includes('New conversation'))!;
    newConversation.click();
    expect(dialogRef.close).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith(['']);
  });

  it('says when only title matches could be returned', () => {
    configure();
    const root = render();
    type(root, 'budget');
    key(root, 'Enter');
    vi.advanceTimersByTime(0);
    respond('all', { results: [], textSearchAvailable: false });
    expect(root.textContent).toContain('Showing title matches only');
  });

  it('a failed search keeps the loaded matches and says so', () => {
    configure();
    const root = render();
    type(root, 'budget');
    vi.advanceTimersByTime(LEXICAL_DEBOUNCE_MS);
    http.expectOne(r => r.params.get('mode') === 'lexical').flush('boom', { status: 500, statusText: 'err' });
    fixture.detectChanges();
    expect(root.textContent).toContain("Couldn't search conversations");
    expect(optionTitles(root)).toEqual(['Budget review']);
    vi.advanceTimersByTime(TEXT_SEARCH_PAUSE_MS);
    http.expectOne(r => r.params.get('mode') === 'all').flush({ results: [], textSearchAvailable: true });
  });

  it('one "No matches" line once every search has settled', () => {
    configure();
    const root = render();
    type(root, 'kubernetes');
    expect(root.textContent).not.toContain('No matches');
    vi.advanceTimersByTime(LEXICAL_DEBOUNCE_MS);
    respond('lexical', { results: [], textSearchAvailable: true });
    vi.advanceTimersByTime(TEXT_SEARCH_PAUSE_MS);
    respond('all', { results: [], textSearchAvailable: true });
    expect(root.textContent).toContain('No matches');
    expect(root.querySelector('[role="listbox"]')).toBeNull();
    expect(input(root).getAttribute('aria-expanded')).toBe('false');
  });

  it('a hand-off from the sidebar searches its query in full at once', () => {
    configure({ query: 'syllabus' });
    const root = render();
    expect(input(root).value).toBe('syllabus');
    vi.advanceTimersByTime(0);
    respond('all', { results: [], textSearchAvailable: true });
  });

  it('keeps the query across a refresh', () => {
    configure();
    const root = render();
    type(root, 'bud');
    expect(sessionStorage.getItem(SEARCH_DIALOG_STORAGE_KEY)).toBe('bud');
    vi.advanceTimersByTime(TEXT_SEARCH_PAUSE_MS);
    http.match(() => true).forEach(r => r.flush({ results: [], textSearchAvailable: true }));
  });

  it('Escape closes', () => {
    configure();
    const root = render();
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(dialogRef.close).toHaveBeenCalled();
  });
});
