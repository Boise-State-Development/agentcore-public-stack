import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';

/** What a conversation without a title is called in the list, and what search matches it by. */
export const UNTITLED_SESSION_TITLE = 'Untitled Session';

/** The query as a title match compares it: trimmed and lower-cased. Empty means "no filter". */
export function normalizeSessionQuery(query: string): string {
  return query.trim().toLowerCase();
}

/**
 * The loaded conversations whose title contains the query, case-insensitively,
 * in the order they were given (the list's recency order). An empty or
 * whitespace-only query returns the input unchanged. The search dialog uses it
 * to show loaded title matches the server does not have yet.
 *
 * Matches the title the row shows, so an untitled conversation is found by
 * "untitled" exactly as it reads in the list.
 */
export function filterSessionsByTitle(sessions: readonly SessionMetadata[], query: string): SessionMetadata[] {
  const needle = normalizeSessionQuery(query);
  if (!needle) return [...sessions];
  return sessions.filter(s => (s.title || UNTITLED_SESSION_TITLE).toLowerCase().includes(needle));
}
