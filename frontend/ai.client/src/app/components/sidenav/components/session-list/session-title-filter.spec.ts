import { describe, it, expect } from 'vitest';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import { filterSessionsByTitle, normalizeSessionQuery } from './session-title-filter';

function session(sessionId: string, title: string): SessionMetadata {
  return {
    sessionId,
    userId: 'user-1',
    title,
    status: 'active',
    createdAt: '2026-10-01T00:00:00Z',
    lastMessageAt: '2026-10-01T00:00:00Z',
    messageCount: 2,
  } as SessionMetadata;
}

describe('filterSessionsByTitle', () => {
  const sessions = [
    session('a', 'BIO 101 syllabus rewrite'),
    session('b', 'Window functions in Postgres'),
    session('c', 'bio lab safety quiz'),
    session('d', ''),
  ];

  it('matches case-insensitively, keeping the given order', () => {
    expect(filterSessionsByTitle(sessions, 'BiO').map(s => s.sessionId)).toEqual(['a', 'c']);
    expect(filterSessionsByTitle(sessions, 'WINDOW FUNCTION').map(s => s.sessionId)).toEqual(['b']);
  });

  it('ignores surrounding whitespace in the query', () => {
    expect(filterSessionsByTitle(sessions, '  postgres  ').map(s => s.sessionId)).toEqual(['b']);
  });

  it('returns every session for an empty or whitespace-only query', () => {
    expect(filterSessionsByTitle(sessions, '')).toEqual(sessions);
    expect(filterSessionsByTitle(sessions, '   ')).toEqual(sessions);
  });

  it('finds an untitled conversation by the label the row shows', () => {
    expect(filterSessionsByTitle(sessions, 'untitled').map(s => s.sessionId)).toEqual(['d']);
  });

  it('returns an empty list when nothing matches', () => {
    expect(filterSessionsByTitle(sessions, 'kubernetes')).toEqual([]);
  });

  it('normalizes the query by trimming and lower-casing', () => {
    expect(normalizeSessionQuery('  Hello World ')).toBe('hello world');
  });
});
