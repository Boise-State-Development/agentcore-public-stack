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
import { ScopeLoadState, SearchScopesService, SearchableAgent } from './search-scopes.service';
import { ProjectsService } from '../../projects/services/projects.service';
import { Project } from '../../projects/models/project.model';
import { LibraryArtifact } from '../../session/services/artifacts/artifact-http.service';

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

const project = (projectId: string, name: string, description = ''): Project => ({
  projectId,
  name,
  description,
  ownerEmail: 'o@example.edu',
  ownerName: null,
  role: 'owner',
  status: 'active',
  editorsManageMembers: false,
  memberCount: 0,
  harnessAgentId: `h-${projectId}`,
  createdAt: iso,
  updatedAt: iso,
});

const agent = (agentId: string, name: string, extra: Partial<SearchableAgent> = {}): SearchableAgent => ({
  agentId,
  name,
  description: '',
  tag: null,
  draft: false,
  ...extra,
});

const artifact = (artifactId: string, title: string, contentType = 'text/markdown'): LibraryArtifact => ({
  artifactId,
  version: 1,
  title,
  contentType,
  createdAt: iso,
  updatedAt: iso,
  sessionId: 's0',
});

describe('SearchDialogComponent', () => {
  let fixture: ComponentFixture<SearchDialogComponent>;
  let http: HttpTestingController;
  let dialogRef: { close: ReturnType<typeof vi.fn> };
  let navigate: ReturnType<typeof vi.spyOn>;
  let sessions: ReturnType<typeof session>[];
  let scopeData: {
    agents: ReturnType<typeof signal<SearchableAgent[]>>;
    agentsState: ReturnType<typeof signal<ScopeLoadState>>;
    artifacts: ReturnType<typeof signal<LibraryArtifact[]>>;
    artifactsState: ReturnType<typeof signal<ScopeLoadState>>;
    loadAgents: ReturnType<typeof vi.fn>;
    loadArtifacts: ReturnType<typeof vi.fn>;
    retryFailed: ReturnType<typeof vi.fn>;
  };
  let projects: {
    projects$: ReturnType<typeof signal<Project[]>>;
    loading$: ReturnType<typeof signal<boolean>>;
    error$: ReturnType<typeof signal<string | null>>;
    available$: ReturnType<typeof signal<boolean | null>>;
    load: ReturnType<typeof vi.fn>;
  };

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
        { provide: SearchScopesService, useValue: scopeData },
        { provide: ProjectsService, useValue: projects },
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
    scopeData = {
      agents: signal<SearchableAgent[]>([]),
      agentsState: signal<ScopeLoadState>('ready'),
      artifacts: signal<LibraryArtifact[]>([]),
      artifactsState: signal<ScopeLoadState>('ready'),
      loadAgents: vi.fn().mockResolvedValue(undefined),
      loadArtifacts: vi.fn().mockResolvedValue(undefined),
      retryFailed: vi.fn(),
    };
    projects = {
      projects$: signal<Project[]>([]),
      loading$: signal(false),
      error$: signal<string | null>(null),
      available$: signal<boolean | null>(true),
      load: vi.fn().mockResolvedValue(undefined),
    };
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

  describe('scopes', () => {
    const tabs = (root: HTMLElement) => Array.from(root.querySelectorAll('[role="tab"]')) as HTMLButtonElement[];
    const selectedTab = (root: HTMLElement) => tabs(root).find(t => t.getAttribute('aria-selected') === 'true')?.textContent?.trim();
    const live = (root: HTMLElement) => root.querySelector('[aria-live="polite"]')?.textContent?.trim();

    function settleConversations(root: HTMLElement): void {
      vi.advanceTimersByTime(LEXICAL_DEBOUNCE_MS);
      respond('lexical', { results: [], textSearchAvailable: true });
      vi.advanceTimersByTime(TEXT_SEARCH_PAUSE_MS);
      http.match(r => r.params.get('mode') === 'all').forEach(r => r.flush({ results: [], textSearchAvailable: true }));
      fixture.detectChanges();
      void root;
    }

    it('chips form a tablist with All selected on open; Projects only when the build has them', () => {
      configure();
      let root = render();
      expect(root.querySelector('[role="tablist"]')).not.toBeNull();
      expect(tabs(root).map(t => t.textContent?.trim())).toEqual(['All', 'Conversations', 'Projects', 'Agents', 'Artifacts']);
      expect(selectedTab(root)).toBe('All');
      expect(tabs(root).map(t => t.tabIndex)).toEqual([0, -1, -1, -1, -1]);
      TestBed.resetTestingModule();

      configure(null, { projects: false, conversationSearch: true });
      root = render();
      expect(tabs(root).map(t => t.textContent?.trim())).toEqual(['All', 'Conversations', 'Agents', 'Artifacts']);
    });

    it('loads agents on open and artifacts only once a query reaches them', () => {
      configure();
      const root = render();
      expect(scopeData.loadAgents).toHaveBeenCalledTimes(1);
      expect(scopeData.loadArtifacts).not.toHaveBeenCalled();
      type(root, 'plan');
      expect(scopeData.loadArtifacts).toHaveBeenCalled();
      settleConversations(root);
    });

    it('All: one capped section per scope with matches, in the fixed order, and Show all selects that scope', () => {
      sessions = Array.from({ length: 7 }, (_, i) => session(`s${i}`, `Plan ${i}`));
      configure();
      projects.projects$.set([project('p1', 'Course plan', 'Fall redesign'), project('p2', 'Other', 'Lesson plan')]);
      scopeData.agents.set([agent('a1', 'Planner', { tag: 'Public' })]);
      scopeData.artifacts.set([artifact('f1', 'Unrelated')]);
      const root = render();
      type(root, 'plan');
      settleConversations(root);

      expect(headings(root)).toEqual(['Conversations', 'Projects', 'Agents']);
      const conversationOptions = Array.from(root.querySelectorAll('#search-section-conversations-label ~ [role="option"]'));
      expect(conversationOptions).toHaveLength(6);
      expect(conversationOptions.at(-1)?.textContent).toContain('Show all 7');
      expect(root.textContent).toContain('Public');
      expect(live(root)).toBe('7 conversations, 2 projects, 1 agent');

      (conversationOptions.at(-1) as HTMLElement).click();
      fixture.detectChanges();
      expect(selectedTab(root)).toBe('Conversations');
      expect(headings(root)).toEqual(['Conversations']);
      expect(options(root)).toHaveLength(7);
      expect(dialogRef.close).not.toHaveBeenCalled();
    });

    it('one "No matches" line when nothing in any scope matches', () => {
      configure();
      projects.projects$.set([project('p1', 'Course plan')]);
      scopeData.agents.set([agent('a1', 'Planner')]);
      const root = render();
      type(root, 'zzz');
      settleConversations(root);
      expect(root.querySelectorAll('[role="group"]')).toHaveLength(0);
      expect(root.textContent?.match(/No matches/g)).toHaveLength(2); // the line and the live region
      expect(live(root)).toBe('No matches');
    });

    it('a scope whose list failed says so and the other scopes still render', () => {
      configure();
      projects.projects$.set([project('p1', 'Budget plan')]);
      scopeData.agentsState.set('error');
      const root = render();
      type(root, 'budget');
      settleConversations(root);
      expect(headings(root)).toEqual(['Conversations', 'Projects']);
      expect(root.textContent).toContain("Couldn't load your agents.");
      expect(root.textContent).not.toContain("Couldn't load your projects.");
    });

    it('nothing counts as settled while a visible scope is still loading', () => {
      configure();
      scopeData.artifactsState.set('loading');
      const root = render();
      type(root, 'zzz');
      settleConversations(root);
      expect(root.textContent).not.toContain('No matches');
      expect(root.textContent).toContain('Searching…');
      scopeData.artifactsState.set('ready');
      fixture.detectChanges();
      expect(live(root)).toBe('No matches');
    });

    it('←/→ change scope only with the caret at an end of the input', () => {
      configure();
      const root = render();
      type(root, 'abc');
      input(root).setSelectionRange(1, 1);
      expect(key(root, 'ArrowRight').defaultPrevented).toBe(false);
      expect(selectedTab(root)).toBe('All');

      input(root).setSelectionRange(3, 3);
      expect(key(root, 'ArrowRight').defaultPrevented).toBe(true);
      expect(selectedTab(root)).toBe('Conversations');

      input(root).setSelectionRange(0, 0);
      key(root, 'ArrowLeft');
      expect(selectedTab(root)).toBe('All');
      key(root, 'ArrowLeft');
      expect(selectedTab(root)).toBe('Artifacts');

      input(root).setSelectionRange(0, 3);
      expect(key(root, 'ArrowLeft').defaultPrevented).toBe(false);
      expect(selectedTab(root)).toBe('Artifacts');
      http.match(() => true).forEach(r => r.flush({ results: [], textSearchAvailable: true }));
    });

    it('arrow keys on a focused chip move and select, roving the tab stop', () => {
      configure();
      const root = render();
      tabs(root)[0].focus();
      tabs(root)[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
      fixture.detectChanges();
      expect(selectedTab(root)).toBe('Conversations');
      expect(document.activeElement).toBe(tabs(root)[1]);
      expect(tabs(root).map(t => t.tabIndex)).toEqual([-1, 0, -1, -1, -1]);
      tabs(root)[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
      fixture.detectChanges();
      expect(selectedTab(root)).toBe('Artifacts');
    });

    it('↑/↓ cross from one section into the next', () => {
      configure();
      projects.projects$.set([project('p1', 'Budget plan')]);
      const root = render();
      type(root, 'budget');
      settleConversations(root);
      key(root, 'ArrowDown');
      key(root, 'ArrowDown');
      const active = root.querySelector(`#${input(root).getAttribute('aria-activedescendant')}`);
      expect(active?.textContent).toContain('Budget plan');
      key(root, 'Enter');
      expect(navigate).toHaveBeenCalledWith(['/projects', 'p1']);
    });

    it('a single scope lists everything without a query, and does not search conversations', () => {
      configure();
      scopeData.agents.set([agent('a1', 'Alpha'), agent('a2', 'Beta')]);
      const root = render();
      tabs(root)[3].click();
      fixture.detectChanges();
      expect(headings(root)).toEqual(['Agents']);
      expect(options(root)).toHaveLength(2);
      type(root, 'beta');
      expect(options(root)).toHaveLength(1);
      vi.advanceTimersByTime(5000);
      http.expectNone(() => true);
      expect(live(root)).toBe('1 agent');
    });

    it('rows open their pages: a project, an agent (a draft in its editor) and an artifact', () => {
      configure();
      projects.projects$.set([project('p1', 'Zeta project')]);
      scopeData.agents.set([agent('a1', 'Zeta agent', { tag: 'Public' }), agent('a2', 'Zeta draft', { draft: true, tag: 'Draft' })]);
      scopeData.artifacts.set([artifact('f1', 'Zeta notes', 'text/csv')]);
      const root = render();
      type(root, 'zeta');
      settleConversations(root);
      expect(root.textContent).toContain('CSV');
      const open = (text: string) => options(root).find(o => o.textContent?.includes(text))!.click();
      open('Zeta project');
      expect(navigate).toHaveBeenLastCalledWith(['/projects', 'p1']);
      open('Zeta agent');
      expect(navigate).toHaveBeenLastCalledWith(['/agents', 'a1']);
      open('Zeta draft');
      expect(navigate).toHaveBeenLastCalledWith(['/agents', 'a2', 'edit']);
      open('Zeta notes');
      expect(navigate).toHaveBeenLastCalledWith(['/artifacts', 'f1']);
    });
  });
});
