import { Injectable, computed, signal } from '@angular/core';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';

/** What a conversation without a title is called in the list, and what the filter matches it by. */
export const UNTITLED_SESSION_TITLE = 'Untitled Session';

/** sessionStorage key for the sidebar filter, so a refresh keeps what the user typed. */
export const SESSION_FILTER_STORAGE_KEY = 'sidenav.conversationFilter';

/** The query as the filter compares it: trimmed and lower-cased. Empty means "no filter". */
export function normalizeSessionQuery(query: string): string {
  return query.trim().toLowerCase();
}

/**
 * The loaded conversations whose title contains the query, case-insensitively,
 * in the order they were given (the list's recency order). An empty or
 * whitespace-only query returns the input unchanged.
 *
 * Matches the title the row shows, so an untitled conversation is found by
 * "untitled" exactly as it reads in the list.
 */
export function filterSessionsByTitle(sessions: readonly SessionMetadata[], query: string): SessionMetadata[] {
  const needle = normalizeSessionQuery(query);
  if (!needle) return [...sessions];
  return sessions.filter(s => (s.title || UNTITLED_SESSION_TITLE).toLowerCase().includes(needle));
}

/**
 * The sidebar's conversation filter query.
 *
 * Root-provided rather than component state because the shell mounts the
 * sidenav twice (the desktop column and the off-canvas drawer), and the two
 * must show the same query. Kept in sessionStorage so a refresh keeps it; every
 * read and write is guarded, because storage can be absent or throw (private
 * windows, blocked site data) and the filter must work without it.
 */
@Injectable({ providedIn: 'root' })
export class SessionTitleFilter {
  private readonly querySignal = signal(readStoredQuery());

  /** What the user typed, as typed. */
  readonly query = this.querySignal.asReadonly();

  /** Whether a query is narrowing the list (whitespace alone does not). */
  readonly isActive = computed(() => normalizeSessionQuery(this.querySignal()) !== '');

  setQuery(query: string): void {
    this.querySignal.set(query);
    writeStoredQuery(query);
  }

  clear(): void {
    this.setQuery('');
  }
}

function readStoredQuery(): string {
  try {
    return globalThis.sessionStorage?.getItem(SESSION_FILTER_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

function writeStoredQuery(query: string): void {
  try {
    if (query) {
      globalThis.sessionStorage?.setItem(SESSION_FILTER_STORAGE_KEY, query);
    } else {
      globalThis.sessionStorage?.removeItem(SESSION_FILTER_STORAGE_KEY);
    }
  } catch {
    // Storage unavailable: the filter still works, it just won't survive a refresh.
  }
}
