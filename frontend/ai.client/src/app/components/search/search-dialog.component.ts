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
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DIALOG_DATA, Dialog, DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { EMPTY, Observable, Subject, catchError, defer, finalize, firstValueFrom, map, merge, switchMap, tap, timer } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroChatBubbleLeftRight,
  heroMagnifyingGlass,
  heroPlus,
  heroRectangleStack,
  heroSparkles,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../dialog/dialog-dismiss.directive';
import { FEATURES } from '../../services/features';
import { SessionService } from '../../session/services/session/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { SessionMetadata } from '../../session/services/models/session-metadata.model';
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

type SearchRow = ConversationRow | ActionRow;

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
 * The conversation-search dialog (`docs/specs/conversation-search.md` §6, §6a),
 * Conversations scope.
 *
 * Empty: the eight most recent loaded conversations and a few actions, from
 * memory, no request. Typing: the loaded conversations filtered by title at
 * once, then the server's title/opening-prompt matches (debounced), then on
 * Enter or a pause the full-text matches with a snippet, each superseding the
 * last. Choosing a conversation opens it at the matching turn (`?m=`).
 *
 * Combobox pattern: focus stays in the input, ↑/↓ move a highlight
 * (`aria-activedescendant`), ↵ opens it — or, with nothing highlighted, runs
 * the full-text search at once.
 */
@Component({
  selector: 'app-search-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, NgIcon],
  providers: [
    provideIcons({
      heroArrowPath,
      heroChatBubbleLeftRight,
      heroMagnifyingGlass,
      heroPlus,
      heroRectangleStack,
      heroSparkles,
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
  private readonly features = inject(FEATURES);
  private readonly injector = inject(Injector);

  private readonly input = viewChild.required<ElementRef<HTMLInputElement>>('searchInput');
  private readonly listbox = viewChild<ElementRef<HTMLElement>>('listbox');

  protected readonly listboxId = 'search-dialog-listbox';

  /** What the user typed, as typed. */
  protected readonly query = signal(this.data?.query ?? readStoredQuery());
  protected readonly normalizedQuery = computed(() => normalizeSessionQuery(this.query()));
  protected readonly hasQuery = computed(() => this.normalizedQuery() !== '');

  private readonly server = signal<ServerResults | null>(null);
  /** Server legs scheduled or in flight for the current query. */
  private readonly pendingLegs = signal(0);
  /** The full-text leg is in flight: the one leg that shows a spinner. */
  protected readonly textPending = signal(false);
  protected readonly error = signal(false);

  /** The highlighted row, or -1 for none (↵ then searches full text). */
  protected readonly activeIndex = signal(-1);

  private readonly sessions = computed(() => this.sessionService.mergedSessionsResource()?.sessions ?? []);

  /** The server's answer, only while it answers what is typed now. */
  private readonly currentServer = computed(() => {
    const server = this.server();
    return server && server.query === this.normalizedQuery() ? server : null;
  });

  private readonly conversationRows = computed<ConversationRow[]>(() => {
    if (!this.hasQuery()) return [];
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

  protected readonly sections = computed<SearchSection[]>(() => {
    let index = 0;
    const section = (id: string, label: string, rows: SearchRow[]): SearchSection => ({
      id,
      label,
      rows: rows.map(row => ({ row, index: index++ })),
    });
    if (!this.hasQuery()) {
      const recent = this.sessions().slice(0, RECENT_LIMIT).map(fromLoadedSession);
      return [
        ...(recent.length ? [section('search-section-recent', 'Recent', recent)] : []),
        section('search-section-actions', 'Actions', this.actions()),
      ];
    }
    const rows = this.conversationRows();
    return rows.length ? [section('search-section-conversations', 'Conversations', rows)] : [];
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
    return server !== null && !server.response.textSearchAvailable;
  });

  protected readonly searching = computed(() => this.pendingLegs() > 0);

  protected readonly showNoMatches = computed(() => this.hasQuery() && !this.hasRows() && !this.searching());

  /** Announced politely once a search settles. */
  protected readonly liveStatus = computed(() => {
    if (!this.hasQuery() || this.searching()) return '';
    const n = this.conversationRows().length;
    if (n === 0) return 'No matches';
    return `${n} ${n === 1 ? 'conversation' : 'conversations'}`;
  });

  /** Keyed so the Recent → results swap replays its crossfade. */
  protected readonly bodyMode = computed(() => [this.hasQuery() ? 'results' : 'empty']);

  private readonly searches = new Subject<{ query: string; now: boolean }>();

  constructor() {
    this.searches
      .pipe(
        switchMap(({ query, now }) => this.searchLegs(query, now)),
        takeUntilDestroyed(inject(DestroyRef)),
      )
      .subscribe(result => this.applyServerResult(result));

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
      case 'Enter': {
        event.preventDefault();
        const row = this.rows()[this.activeIndex()];
        if (row) {
          void this.activate(row);
        } else if (this.hasQuery()) {
          this.searches.next({ query: this.query(), now: true });
        }
        break;
      }
    }
  }

  protected onRowPointer(index: number): void {
    if (this.activeIndex() !== index) this.activeIndex.set(index);
  }

  protected async activate(row: SearchRow): Promise<void> {
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

  private setQuery(value: string, now: boolean): void {
    this.query.set(value);
    this.activeIndex.set(-1);
    writeStoredQuery(value);
    this.searches.next({ query: value, now });
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
    this.error.set(false);
    if (!normalized) return EMPTY;

    const leg = (mode: ConversationSearchMode, delay: number): Observable<ServerResults> =>
      defer(() => {
        this.pendingLegs.update(n => n + 1);
        return timer(delay).pipe(
          tap(() => mode === 'all' && this.textPending.set(true)),
          switchMap(() => this.api.search(trimmed, mode)),
          map(response => ({ query: normalized, mode, response })),
          catchError(() => {
            this.error.set(true);
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
