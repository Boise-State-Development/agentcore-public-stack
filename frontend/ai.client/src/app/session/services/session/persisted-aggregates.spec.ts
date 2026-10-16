import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { computed, effect, signal } from '@angular/core';
import { ChatStateService } from '../chat/chat-state.service';
import { SessionMetadata } from '../models/session-metadata.model';
import { persistedAggregatesOf, samePersistedAggregates } from './persisted-aggregates';

const optimisticRow = (sessionId: string): SessionMetadata => ({
  sessionId,
  userId: 'u1',
  title: 'New Conversation',
  status: 'active',
  createdAt: '2026-10-15T00:00:00Z',
  lastMessageAt: '2026-10-15T00:00:00Z',
  messageCount: 0,
});

describe('persistedAggregatesOf', () => {
  it('is null without a session', () => {
    expect(persistedAggregatesOf(null)).toBeNull();
    expect(persistedAggregatesOf({ ...optimisticRow(''), sessionId: '' })).toBeNull();
  });

  it('keeps only the session id and its aggregates', () => {
    const row = { ...optimisticRow('s1'), totalCost: 0.5, lastContextTokens: 10, contextWindow: 200, preferences: { lastModel: 'm' } };
    expect(persistedAggregatesOf(row)).toEqual({
      sessionId: 's1',
      totalCost: 0.5,
      lastContextTokens: 10,
      contextWindow: 200,
    });
  });
});

describe('samePersistedAggregates', () => {
  const base = persistedAggregatesOf({ ...optimisticRow('s1'), totalCost: 0.5, lastContextTokens: 10, contextWindow: 200 });

  it('ignores fields other than the aggregates', () => {
    const renamed = persistedAggregatesOf({ ...optimisticRow('s1'), title: 'Renamed', totalCost: 0.5, lastContextTokens: 10, contextWindow: 200 });
    expect(samePersistedAggregates(base, renamed)).toBe(true);
  });

  it('differs when any aggregate or the session changes', () => {
    expect(samePersistedAggregates(base, { ...base!, totalCost: 0.6 })).toBe(false);
    expect(samePersistedAggregates(base, { ...base!, lastContextTokens: 11 })).toBe(false);
    expect(samePersistedAggregates(base, { ...base!, contextWindow: 272000 })).toBe(false);
    expect(samePersistedAggregates(base, { ...base!, sessionId: 's2' })).toBe(false);
    expect(samePersistedAggregates(base, null)).toBe(false);
  });
});

// The session page's wiring, against the real ChatStateService: the badge is
// seeded from the row, then a live turn adds to it over SSE.
describe('seeding the cost badge from the session row', () => {
  let chatState: ChatStateService;
  let currentSession: ReturnType<typeof signal<SessionMetadata>>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    chatState = TestBed.inject(ChatStateService);
    currentSession = signal<SessionMetadata>(optimisticRow('s1'));
    TestBed.runInInjectionContext(() => {
      const aggregates = computed(() => persistedAggregatesOf(currentSession()), { equal: samePersistedAggregates });
      effect(() => {
        const value = aggregates();
        if (!value) return;
        chatState.seedSessionAggregates(value.sessionId, value);
      });
    });
    chatState.setViewedSession('s1');
    TestBed.tick();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  const liveFirstTurn = () => {
    chatState.addTurnCost('s1', 0.0402);
    chatState.setContext('s1', 29204, 200000);
  };

  it("keeps a first turn's live cost and context when the preference read-back rewrites the row", () => {
    liveFirstTurn();

    // refreshNewSessionFromServer -> adoptServerPreferences on stream close.
    currentSession.update(row => ({ ...row, preferences: { lastModel: 'us.openai.gpt-6-luna' } }));
    TestBed.tick();

    expect(chatState.costDollars()).toBeCloseTo(0.0402);
    expect(chatState.contextTokens()).toBe(29204);
    expect(chatState.contextWindowSize()).toBe(200000);
  });

  it('keeps live values through a title sync', () => {
    liveFirstTurn();

    currentSession.update(row => ({ ...row, title: 'Greeting' }));
    TestBed.tick();

    expect(chatState.costDollars()).toBeCloseTo(0.0402);
    expect(chatState.contextTokens()).toBe(29204);
  });

  it('re-seeds when the row brings new aggregates from the server', () => {
    liveFirstTurn();

    currentSession.set({ ...optimisticRow('s1'), totalCost: 0.05, lastContextTokens: 31000, contextWindow: 200000 });
    TestBed.tick();

    expect(chatState.costDollars()).toBeCloseTo(0.05);
    expect(chatState.contextTokens()).toBe(31000);
  });

  it('seeds a different session when the row switches to it', () => {
    liveFirstTurn();

    currentSession.set({ ...optimisticRow('s2'), totalCost: 1.25, lastContextTokens: 500, contextWindow: 272000 });
    chatState.setViewedSession('s2');
    TestBed.tick();

    expect(chatState.costDollars()).toBeCloseTo(1.25);
    expect(chatState.contextWindowSize()).toBe(272000);

    // s1's live totals are untouched.
    chatState.setViewedSession('s1');
    expect(chatState.costDollars()).toBeCloseTo(0.0402);
  });
});
