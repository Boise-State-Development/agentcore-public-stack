import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import {
  SESSION_FILTER_STORAGE_KEY,
  SessionTitleFilter,
  filterSessionsByTitle,
  normalizeSessionQuery,
} from './session-title-filter';

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

describe('SessionTitleFilter', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    sessionStorage.removeItem(SESSION_FILTER_STORAGE_KEY);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.removeItem(SESSION_FILTER_STORAGE_KEY);
    TestBed.resetTestingModule();
  });

  it('starts empty and inactive', () => {
    const filter = TestBed.inject(SessionTitleFilter);
    expect(filter.query()).toBe('');
    expect(filter.isActive()).toBe(false);
  });

  it('treats a whitespace-only query as inactive', () => {
    const filter = TestBed.inject(SessionTitleFilter);
    filter.setQuery('   ');
    expect(filter.isActive()).toBe(false);
  });

  it('survives a refresh through sessionStorage', () => {
    TestBed.inject(SessionTitleFilter).setQuery('syllabus');
    expect(sessionStorage.getItem(SESSION_FILTER_STORAGE_KEY)).toBe('syllabus');

    // A fresh injector stands in for a reloaded page.
    TestBed.resetTestingModule();
    const reloaded = TestBed.inject(SessionTitleFilter);
    expect(reloaded.query()).toBe('syllabus');
    expect(reloaded.isActive()).toBe(true);
  });

  it('removes the stored query when cleared', () => {
    const filter = TestBed.inject(SessionTitleFilter);
    filter.setQuery('syllabus');
    filter.clear();
    expect(filter.query()).toBe('');
    expect(sessionStorage.getItem(SESSION_FILTER_STORAGE_KEY)).toBeNull();
  });

  it('works without storage when reading throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    const filter = TestBed.inject(SessionTitleFilter);
    expect(filter.query()).toBe('');
  });

  it('keeps filtering when writing throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    const filter = TestBed.inject(SessionTitleFilter);
    expect(() => filter.setQuery('bio')).not.toThrow();
    expect(filter.query()).toBe('bio');
    expect(filter.isActive()).toBe(true);
  });
});
