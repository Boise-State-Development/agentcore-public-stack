import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  signal,
  untracked,
  viewChild,
  viewChildren,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DIALOG_DATA, Dialog, DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { EMPTY, Observable, Subject, catchError, defer, finalize, firstValueFrom, map, merge, switchMap, tap, timer } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroArrowRight,
  heroChatBubbleLeftRight,
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroFolder,
  heroMagnifyingGlass,
  heroPhoto,
  heroPlus,
  heroRectangleStack,
  heroSparkles,
  heroTableCells,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../dialog/dialog-dismiss.directive';
import { FEATURES } from '../../services/features';
import { SessionService } from '../../session/services/session/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { SessionMetadata } from '../../session/services/models/session-metadata.model';
import { ProjectsService } from '../../projects/services/projects.service';
import { Project } from '../../projects/models/project.model';
import { LibraryArtifact } from '../../session/services/artifacts/artifact-http.service';
import { artifactTypeStyle } from '../../artifacts/artifact-type-style';
import {
  UNTITLED_SESSION_TITLE,
  filterSessionsByTitle,
  normalizeSessionQuery,
} from '../sidenav/components/session-list/session-title-filter';
import {
  ConversationSearchApiService,
  ConversationSearchMode,
  ConversationSearchResponse,
  ConversationSearchResult,
} from './conversation-search-api.service';
import { SearchDialogData, SearchDialogService } from './search-dialog.service';
import { ScopeLoadState, SearchAgentTag, SearchScopesService, SearchableAgent } from './search-scopes.service';
import { HighlightSegment, formatLastMoved, highlightSegments } from './search-text';

/** sessionStorage key for the dialog's query; separate from the sidebar filter's. */
export const SEARCH_DIALOG_STORAGE_KEY = 'search.conversationQuery';

/** How many recent conversations the empty state lists (§6a). */
export const RECENT_LIMIT = 8;

/** Debounce before the title/opening-prompt search (every keystroke). */
export const LEXICAL_DEBOUNCE_MS = 250;

/** Pause before the full-text search; it is ~700 ms, so never per keystroke (§5). */
export const TEXT_SEARCH_PAUSE_MS = 600;

/** Shorter queries never reach the full-text search. */
export const TEXT_SEARCH_MIN_CHARS = 3;

/** Rows per section in the All view before "Show all N" (§6a). */
export const SECTION_CAP = 5;

/** A scope chip. `all` shows a capped section per scope that has matches. */
export type SearchScope = 'all' | ResultScope;

/** A scope that has results of its own, in the fixed order sections render. */
export type ResultScope = 'conversations' | 'projects' | 'agents' | 'artifacts';

const RESULT_SCOPES: readonly ResultScope[] = ['conversations', 'projects', 'agents', 'artifacts'];

const SCOPE_LABELS: Record<SearchScope, string> = {
  all: 'All',
  conversations: 'Conversations',
  projects: 'Projects',
  agents: 'Agents',
  artifacts: 'Artifacts',
};

/** Singular and plural, for "12 conversations, 2 projects". */
const SCOPE_NOUNS: Record<ResultScope, [string, string]> = {
  conversations: ['conversation', 'conversations'],
  projects: ['project', 'projects'],
  agents: ['agent', 'agents'],
  artifacts: ['artifact', 'artifacts'],
};

const SCOPE_ERRORS: Record<ResultScope, string> = {
  conversations: "Couldn't search conversations. Loaded titles are still shown.",
  projects: "Couldn't load your projects.",
  agents: "Couldn't load your agents.",
  artifacts: "Couldn't load your artifacts.",
};

interface ConversationRow {
  kind: 'conversation';
  key: string;
  sessionId: string;
  title: string;
  lastMessageAt: string;
  snippet: string;
  archived: boolean;
  messageId: string | null;
  assistantId: string | null;
  /** The best turn plus any `alsoMatched` ones; 0 for a title-only row. */
  matchCount: number;
}

interface ActionRow {
  kind: 'action';
  key: string;
  label: string;
  icon: string;
  action: 'new-conversation' | 'new-project' | 'new-agent';
}

interface ProjectRow {
  kind: 'project';
  key: string;
  projectId: string;
  name: string;
  description: string;
  archived: boolean;
}

interface AgentRow {
  kind: 'agent';
  key: string;
  agentId: string;
  name: string;
  description: string;
  tag: SearchAgentTag | null;
  draft: boolean;
}

interface ArtifactRow {
  kind: 'artifact';
  key: string;
  artifactId: string;
  title: string;
  typeLabel: string;
  icon: string;
  updatedAt: string;
}

/** "Show all N": the last row of a capped section, which selects its scope. */
interface MoreRow {
  kind: 'more';
  key: string;
  scope: ResultScope;
  label: string;
}

type SearchRow = ConversationRow | ActionRow | ProjectRow | AgentRow | ArtifactRow | MoreRow;

interface SearchSection {
  id: string;
  label: string;
  rows: { row: SearchRow; index: number }[];
}

interface ServerResults {
  /** The normalized query the response answers. */
  query: string;
  mode: ConversationSearchMode;
  response: ConversationSearchResponse;
}

function readStoredQuery(): string {
  try {
    return globalThis.sessionStorage?.getItem(SEARCH_DIALOG_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

function writeStoredQuery(query: string): void {
  try {
    if (query) {
      globalThis.sessionStorage?.setItem(SEARCH_DIALOG_STORAGE_KEY, query);
    } else {
      globalThis.sessionStorage?.removeItem(SEARCH_DIALOG_STORAGE_KEY);
    }
  } catch {
    // Storage unavailable: the query just won't survive a refresh.
  }
}

function fromLoadedSession(session: SessionMetadata): ConversationRow {
  return {
    kind: 'conversation',
    key: `c:${session.sessionId}`,
    sessionId: session.sessionId,
    title: session.title || UNTITLED_SESSION_TITLE,
    lastMessageAt: session.lastMessageAt || session.createdAt,
    snippet: '',
    archived: session.status === 'archived',
    messageId: null,
    assistantId: session.preferences?.assistantId ?? null,
    matchCount: 0,
  };
}

function fromProject(project: Project): ProjectRow {
  return {
    kind: 'project',
    key: `p:${project.projectId}`,
    projectId: project.projectId,
    name: project.name,
    description: project.description,
    archived: project.status === 'archived',
  };
}

function fromAgent(agent: SearchableAgent): AgentRow {
  return { kind: 'agent', key: `g:${agent.agentId}`, ...agent };
}

function fromArtifact(artifact: LibraryArtifact): ArtifactRow {
  const style = artifactTypeStyle(artifact.contentType);
  return {
    kind: 'artifact',
    key: `f:${artifact.artifactId}`,
    artifactId: artifact.artifactId,
    title: artifact.title,
    typeLabel: style.label,
    icon: style.icon,
    updatedAt: artifact.updatedAt,
  };
}

/** Whether any field contains the normalized query; an empty query matches everything. */
function matches(needle: string, ...fields: (string | null | undefined)[]): boolean {
  return !needle || fields.some(field => (field ?? '').toLowerCase().includes(needle));
}

function fromSearchResult(result: ConversationSearchResult): ConversationRow {
  return {
    kind: 'conversation',
    key: `c:${result.sessionId}`,
    sessionId: result.sessionId,
    title: result.title || UNTITLED_SESSION_TITLE,
    lastMessageAt: result.lastMessageAt,
    snippet: result.snippet,
    archived: result.archived,
    messageId: result.messageId ?? null,
    assistantId: result.assistantId ?? null,
    matchCount: result.messageId ? 1 + result.alsoMatched.length : 0,
  };
}

/**
 * The search dialog (`docs/specs/conversation-search.md` §6, §6a).
 *
 * Empty: the eight most recent loaded conversations and a few actions, from
 * memory, no request. Typing: every scope at once, one section per scope that
 * has matches, in a fixed order, each capped with a "Show all N" row; a scope
 * chip narrows to one full list.
 *
 * Conversations: the loaded titles at once, then the server's
 * title/opening-prompt matches (debounced), then on Enter or a pause the
 * full-text matches with a snippet, each superseding the last. Choosing one
 * opens it at the matching turn (`?m=`). Projects, Agents and Artifacts are
 * filtered client-side over lists the app already reads (`SearchScopesService`,
 * `ProjectsService`); a scope whose list failed says so and leaves the rest be.
 *
 * Combobox pattern: focus stays in the input, ↑/↓ move a highlight across
 * sections (`aria-activedescendant`), ↵ opens it — or, with nothing
 * highlighted, runs the full-text search at once — and ←/→ at either end of
 * the input change scope.
 */
@Component({
  selector: 'app-search-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, NgIcon],
  providers: [
    provideIcons({
      heroArrowPath,
      heroArrowRight,
      heroChatBubbleLeftRight,
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroFolder,
      heroMagnifyingGlass,
      heroPhoto,
      heroPlus,
      heroRectangleStack,
      heroSparkles,
      heroTableCells,
      heroXMark,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'close()',
  },
  templateUrl: './search-dialog.component.html',
  styleUrl: './search-dialog.component.css',
})
export class SearchDialogComponent {
  private readonly dialogRef = inject<DialogRef<undefined>>(DialogRef);
  private readonly data = inject<SearchDialogData | null>(DIALOG_DATA, { optional: true });
  private readonly dialog = inject(Dialog);
  private readonly router = inject(Router);
  private readonly api = inject(ConversationSearchApiService);
  private readonly sessionService = inject(SessionService);
  private readonly sidenavService = inject(SidenavService);
  private readonly searchDialog = inject(SearchDialogService);
  private readonly projectsService = inject(ProjectsService);
  private readonly scopeData = inject(SearchScopesService);
  private readonly features = inject(FEATURES);
  private readonly injector = inject(Injector);

  private readonly input = viewChild.required<ElementRef<HTMLInputElement>>('searchInput');
  private readonly listbox = viewChild<ElementRef<HTMLElement>>('listbox');
  private readonly scopeTabs = viewChildren<ElementRef<HTMLButtonElement>>('scopeTab');

  protected readonly listboxId = 'search-dialog-listbox';
  protected readonly panelId = 'search-dialog-panel';

  /** The chips, in order; Projects only in a build that has them. */
  protected readonly scopes: readonly SearchScope[] = [
    'all',
    ...RESULT_SCOPES.filter(scope => scope !== 'projects' || this.features.projects),
  ];

  /** The selected chip. All on every open (§6a). */
  protected readonly scope = signal<SearchScope>('all');

  /** What the user typed, as typed. */
  protected readonly query = signal(this.data?.query ?? readStoredQuery());
  protected readonly normalizedQuery = computed(() => normalizeSessionQuery(this.query()));
  protected readonly hasQuery = computed(() => this.normalizedQuery() !== '');

  private readonly server = signal<ServerResults | null>(null);
  /** Server legs scheduled or in flight for the current query. */
  private readonly pendingLegs = signal(0);
  /** The full-text leg is in flight: the one leg that shows a spinner. */
  protected readonly textPending = signal(false);
  private readonly conversationError = signal(false);

  /** The highlighted row, or -1 for none (↵ then searches full text). */
  protected readonly activeIndex = signal(-1);

  private readonly sessions = computed(() => this.sessionService.mergedSessionsResource()?.sessions ?? []);

  /** Whether the conversation results are on screen: All, or the Conversations chip. */
  protected readonly conversationsVisible = computed(() => this.scope() === 'all' || this.scope() === 'conversations');

  /** The server's answer, only while it answers what is typed now. */
  private readonly currentServer = computed(() => {
    const server = this.server();
    return server && server.query === this.normalizedQuery() ? server : null;
  });

  private readonly conversationRows = computed<ConversationRow[]>(() => {
    if (!this.hasQuery()) return this.sessions().map(fromLoadedSession);
    const server = this.currentServer();
    const local = filterSessionsByTitle(this.sessions(), this.query()).map(fromLoadedSession);
    if (!server) return local;
    // Server matches lead (they reach pages the sidebar has not loaded); a loaded
    // title match the server lacks (a row written before titleLower, an
    // untitled conversation found by "untitled") still shows, once.
    const rows = server.response.results.map(fromSearchResult);
    const seen = new Set(rows.map(r => r.sessionId));
    return [...rows, ...local.filter(r => !seen.has(r.sessionId))];
  });

  private readonly projectRows = computed<ProjectRow[]>(() => {
    if (!this.features.projects) return [];
    const needle = this.normalizedQuery();
    return this.projectsService
      .projects$()
      .filter(p => matches(needle, p.name, p.description))
      .map(fromProject);
  });

  private readonly agentRows = computed<AgentRow[]>(() => {
    const needle = this.normalizedQuery();
    return this.scopeData
      .agents()
      .filter(a => matches(needle, a.name, a.description))
      .map(fromAgent);
  });

  private readonly artifactRows = computed<ArtifactRow[]>(() => {
    const needle = this.normalizedQuery();
    return this.scopeData
      .artifacts()
      .filter(a => matches(needle, a.title))
      .map(fromArtifact);
  });

  private readonly actions = computed<ActionRow[]>(() => {
    const actions: ActionRow[] = [
      { kind: 'action', key: 'a:new-conversation', label: 'New conversation', icon: 'heroPlus', action: 'new-conversation' },
    ];
    if (this.features.projects) {
      actions.push({ kind: 'action', key: 'a:new-project', label: 'New project', icon: 'heroRectangleStack', action: 'new-project' });
    }
    actions.push({ kind: 'action', key: 'a:new-agent', label: 'New agent', icon: 'heroSparkles', action: 'new-agent' });
    return actions;
  });

  /** The scopes whose results the current chip shows. */
  private readonly visibleScopes = computed<readonly ResultScope[]>(() => {
    const scope = this.scope();
    if (scope !== 'all') return [scope];
    return RESULT_SCOPES.filter(s => s !== 'projects' || this.features.projects);
  });

  /** Whether the body shows results rather than Recent + Actions. */
  protected readonly showsResults = computed(() => this.hasQuery() || this.scope() !== 'all');

  private rowsFor(scope: ResultScope): SearchRow[] {
    switch (scope) {
      case 'conversations':
        return this.conversationRows();
      case 'projects':
        return this.projectRows();
      case 'agents':
        return this.agentRows();
      case 'artifacts':
        return this.artifactRows();
    }
  }

  private scopeState(scope: ResultScope): ScopeLoadState {
    switch (scope) {
      case 'conversations':
        if (this.conversationError()) return 'error';
        return this.pendingLegs() > 0 ? 'loading' : 'ready';
      case 'projects':
        if (this.projectsService.error$()) return 'error';
        return this.projectsService.loading$() ? 'loading' : 'ready';
      case 'agents':
        return this.scopeData.agentsState();
      case 'artifacts':
        return this.scopeData.artifactsState();
    }
  }

  protected readonly sections = computed<SearchSection[]>(() => {
    let index = 0;
    const section = (id: string, label: string, rows: SearchRow[]): SearchSection => ({
      id,
      label,
      rows: rows.map(row => ({ row, index: index++ })),
    });
    if (!this.showsResults()) {
      const recent = this.sessions().slice(0, RECENT_LIMIT).map(fromLoadedSession);
      return [
        ...(recent.length ? [section('search-section-recent', 'Recent', recent)] : []),
        section('search-section-actions', 'Actions', this.actions()),
      ];
    }
    const capped = this.scope() === 'all';
    const sections: SearchSection[] = [];
    for (const scope of this.visibleScopes()) {
      const rows = this.rowsFor(scope);
      if (!rows.length) continue;
      const shown = capped && rows.length > SECTION_CAP ? rows.slice(0, SECTION_CAP) : rows;
      if (shown.length < rows.length) {
        shown.push({ kind: 'more', key: `more:${scope}`, scope, label: `Show all ${rows.length}` });
      }
      sections.push(section(`search-section-${scope}`, SCOPE_LABELS[scope], shown));
    }
    return sections;
  });

  private readonly rows = computed(() => this.sections().flatMap(s => s.rows.map(r => r.row)));

  protected readonly hasRows = computed(() => this.rows().length > 0);

  protected readonly activeOptionId = computed(() => {
    const index = this.activeIndex();
    return index >= 0 && index < this.rows().length ? this.optionId(index) : null;
  });

  /** "Showing title matches only": full text was asked for and could not be had. */
  protected readonly titleMatchesOnly = computed(() => {
    const server = this.currentServer();
    return this.conversationsVisible() && server !== null && !server.response.textSearchAvailable;
  });

  /** One line per visible scope whose search or list failed; the others still render. */
  protected readonly scopeErrors = computed(() =>
    this.showsResults()
      ? this.visibleScopes()
          .filter(scope => this.scopeState(scope) === 'error')
          .map(scope => ({ scope, message: SCOPE_ERRORS[scope] }))
      : [],
  );

  /** Some visible scope has not answered yet. An unrequested list counts: it is about to be. */
  protected readonly searching = computed(() =>
    this.visibleScopes().some(scope => {
      const state = this.scopeState(scope);
      return state === 'loading' || state === 'idle';
    }),
  );

  protected readonly showNoMatches = computed(() => this.showsResults() && !this.hasRows() && !this.searching());

  protected readonly noMatchesText = computed(() => {
    const scope = this.scope();
    return this.hasQuery() || scope === 'all' ? 'No matches' : `No ${SCOPE_LABELS[scope].toLowerCase()} yet`;
  });

  /** Announced politely once a search settles: "12 conversations, 2 projects". */
  protected readonly liveStatus = computed(() => {
    if (!this.showsResults() || this.searching()) return '';
    const parts = this.visibleScopes()
      .map(scope => {
        const n = this.rowsFor(scope).length;
        const [one, many] = SCOPE_NOUNS[scope];
        return n ? `${n} ${n === 1 ? one : many}` : '';
      })
      .filter(Boolean);
    return parts.length ? parts.join(', ') : this.noMatchesText();
  });

  /** Keyed so the Recent → results swap, and a scope change, replay the crossfade. */
  protected readonly bodyMode = computed(() => [`${this.showsResults() ? 'results' : 'empty'}:${this.scope()}`]);

  protected readonly placeholder = computed(() => {
    const scope = this.scope();
    return scope === 'all' ? 'Search' : `Search ${SCOPE_LABELS[scope].toLowerCase()}`;
  });

  private readonly searches = new Subject<{ query: string; now: boolean }>();

  constructor() {
    this.searches
      .pipe(
        switchMap(({ query, now }) => this.searchLegs(query, now)),
        takeUntilDestroyed(inject(DestroyRef)),
      )
      .subscribe(result => this.applyServerResult(result));

    // Lists fetched once per page load; one that failed on an earlier open tries again.
    this.scopeData.retryFailed();
    void this.scopeData.loadAgents();
    if (
      this.features.projects &&
      (this.projectsService.available$() === null || this.projectsService.error$()) &&
      !this.projectsService.loading$()
    ) {
      void this.projectsService.load();
    }

    // Artifacts are the one list fetched only once it is asked for (§6a).
    effect(() => {
      const scope = this.scope();
      if (scope === 'artifacts' || (scope === 'all' && this.hasQuery())) {
        untracked(() => void this.scopeData.loadArtifacts());
      }
    });

    // A second open while this one is up: refocus, adopting the handed-off query.
    let handledRefocus = untracked(() => this.searchDialog.refocus()?.seq ?? 0);
    effect(() => {
      const request = this.searchDialog.refocus();
      if (!request || request.seq === handledRefocus) return;
      handledRefocus = request.seq;
      untracked(() => {
        if (request.query !== undefined && request.query !== this.query()) {
          this.setQuery(request.query, true);
        }
        this.focusInput();
      });
    });

    // Keep the highlighted row in view as ↑/↓ move it.
    effect(() => {
      const id = this.activeOptionId();
      if (!id) return;
      untracked(() => {
        afterNextRender(
          () => this.listbox()?.nativeElement.querySelector(`#${CSS.escape(id)}`)?.scrollIntoView({ block: 'nearest' }),
          { injector: this.injector },
        );
      });
    });

    afterNextRender(() => this.focusInput(true));

    const initial = this.query();
    if (normalizeSessionQuery(initial)) {
      // A hand-off came from Enter in the sidebar: search the full text at once.
      this.searches.next({ query: initial, now: this.data?.query !== undefined });
    }
  }

  protected scopeLabel(scope: SearchScope): string {
    return SCOPE_LABELS[scope];
  }

  protected scopeTabId(scope: SearchScope): string {
    return `search-dialog-scope-${scope}`;
  }

  protected optionId(index: number): string {
    return `search-dialog-option-${index}`;
  }

  protected segments(text: string): HighlightSegment[] {
    return highlightSegments(text, this.query());
  }

  protected lastMoved(iso: string): string {
    return formatLastMoved(iso);
  }

  protected onInput(value: string): void {
    this.setQuery(value, false);
  }

  protected clearQuery(): void {
    this.setQuery('', false);
    this.focusInput();
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.isComposing) return;
    const count = this.rows().length;
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (count) this.activeIndex.update(i => (i + 1) % count);
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (count) this.activeIndex.update(i => (i <= 0 ? count - 1 : i - 1));
        break;
      case 'ArrowLeft':
      case 'ArrowRight': {
        // Only at the caret's end of the field: anywhere else the arrows move the caret.
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const field = event.target as HTMLInputElement;
        const start = field.selectionStart ?? 0;
        const end = field.selectionEnd ?? 0;
        if (start !== end) return;
        const back = event.key === 'ArrowLeft';
        if (back ? start !== 0 : end !== field.value.length) return;
        event.preventDefault();
        this.stepScope(back ? -1 : 1);
        break;
      }
      case 'Enter': {
        event.preventDefault();
        const row = this.rows()[this.activeIndex()];
        if (row) {
          void this.activate(row);
        } else if (this.hasQuery() && this.conversationsVisible()) {
          this.searches.next({ query: this.query(), now: true });
        }
        break;
      }
    }
  }

  /** The tablist's own keys (focus is on a chip): arrows, Home and End move and select. */
  protected onScopeKeydown(event: KeyboardEvent): void {
    const last = this.scopes.length - 1;
    const current = this.scopes.indexOf(this.scope());
    let next: number;
    switch (event.key) {
      case 'ArrowLeft':
        next = current <= 0 ? last : current - 1;
        break;
      case 'ArrowRight':
        next = current >= last ? 0 : current + 1;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = last;
        break;
      default:
        return;
    }
    event.preventDefault();
    this.setScope(this.scopes[next]);
    this.scopeTabs()[next]?.nativeElement.focus();
  }

  /** A chip clicked with a pointer hands focus back to the input, so typing carries on. */
  protected onScopeClick(scope: SearchScope, event: MouseEvent): void {
    this.setScope(scope);
    if (event.detail > 0) this.focusInput();
  }

  protected onRowPointer(index: number): void {
    if (this.activeIndex() !== index) this.activeIndex.set(index);
  }

  protected async activate(row: SearchRow): Promise<void> {
    if (row.kind === 'more') {
      this.setScope(row.scope);
      this.focusInput();
      return;
    }
    if (row.kind === 'conversation') {
      const queryParams: Record<string, string> = {};
      if (row.assistantId) queryParams['assistantId'] = row.assistantId;
      if (row.messageId) queryParams['m'] = row.messageId;
      const loaded = this.sessions().find(s => s.sessionId === row.sessionId);
      if (loaded) this.sessionService.currentSession.set(loaded);
      this.close();
      this.sidenavService.close();
      await this.router.navigate(['/s', row.sessionId], { queryParams });
      return;
    }

    this.close();
    this.sidenavService.close();
    switch (row.kind) {
      case 'project':
        await this.router.navigate(['/projects', row.projectId]);
        return;
      case 'agent':
        // A draft has no page worth landing on yet; its editor is where it is finished.
        await this.router.navigate(row.draft ? ['/agents', row.agentId, 'edit'] : ['/agents', row.agentId]);
        return;
      case 'artifact':
        await this.router.navigate(['/artifacts', row.artifactId]);
        return;
    }
    switch (row.action) {
      case 'new-conversation':
        await this.router.navigate(['']);
        break;
      case 'new-agent':
        await this.router.navigate(['/agents/new']);
        break;
      case 'new-project': {
        const { CreateProjectDialogComponent } = await import('../../projects/components/create-project-dialog.component');
        const ref = this.dialog.open<{ projectId: string } | undefined>(CreateProjectDialogComponent);
        const project = await firstValueFrom(ref.closed);
        if (project) await this.router.navigate(['/projects', project.projectId]);
        break;
      }
    }
  }

  close(): void {
    this.dialogRef.close(undefined);
  }

  private stepScope(step: number): void {
    const count = this.scopes.length;
    const current = this.scopes.indexOf(this.scope());
    this.setScope(this.scopes[(current + step + count) % count]);
  }

  private setScope(scope: SearchScope): void {
    if (scope === this.scope()) return;
    this.scope.set(scope);
    this.activeIndex.set(-1);
    // Conversation searches run only while their results are on screen; coming
    // back to them with a query they have not answered picks it up again.
    if (this.conversationsVisible() && this.hasQuery() && !this.currentServer() && this.pendingLegs() === 0) {
      this.searches.next({ query: this.query(), now: false });
    }
  }

  private setQuery(value: string, now: boolean): void {
    this.query.set(value);
    this.activeIndex.set(-1);
    writeStoredQuery(value);
    // Outside the conversation scopes an empty query cancels any leg in flight.
    this.searches.next({ query: this.conversationsVisible() ? value : '', now });
  }

  private focusInput(select = false): void {
    const element = this.input().nativeElement;
    element.focus();
    if (select) element.select();
  }

  /**
   * The server legs for one query: title/opening-prompt after a short debounce,
   * then full text after a pause (or both at once on Enter, where the full-text
   * call already includes the title matches). A newer query cancels both.
   */
  private searchLegs(query: string, now: boolean): Observable<ServerResults> {
    const trimmed = query.trim();
    const normalized = normalizeSessionQuery(query);
    this.conversationError.set(false);
    if (!normalized) return EMPTY;

    const leg = (mode: ConversationSearchMode, delay: number): Observable<ServerResults> =>
      defer(() => {
        this.pendingLegs.update(n => n + 1);
        return timer(delay).pipe(
          tap(() => mode === 'all' && this.textPending.set(true)),
          switchMap(() => this.api.search(trimmed, mode)),
          map(response => ({ query: normalized, mode, response })),
          catchError(() => {
            this.conversationError.set(true);
            return EMPTY;
          }),
          finalize(() => {
            this.pendingLegs.update(n => n - 1);
            if (mode === 'all') this.textPending.set(false);
          }),
        );
      });

    const lexical = now ? EMPTY : leg('lexical', LEXICAL_DEBOUNCE_MS);
    const text = trimmed.length >= TEXT_SEARCH_MIN_CHARS ? leg('all', now ? 0 : TEXT_SEARCH_PAUSE_MS) : EMPTY;
    return merge(lexical, text);
  }

  private applyServerResult(result: ServerResults): void {
    const current = this.server();
    // A late title-only answer never replaces the full-text one for the same query.
    if (result.mode === 'lexical' && current?.query === result.query && current.mode === 'all') return;
    this.server.set(result);
  }
}
