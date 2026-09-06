import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ChatStateService } from './chat-state.service';

describe('ChatStateService', () => {
  let service: ChatStateService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ChatStateService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('viewed-session facades', () => {
    it('default to inert values when no session is viewed', () => {
      expect(service.viewedSessionId()).toBeNull();
      expect(service.isChatLoading()).toBe(false);
      expect(service.currentStopReason()).toBeNull();
      expect(service.lastTurnContinuable()).toBe(false);
      expect(service.costDollars()).toBe(0);
      expect(service.contextTokens()).toBe(0);
      expect(service.contextPct()).toBe(0);
    });

    it('project the viewed session state and follow view changes', () => {
      service.setChatLoading('a', true);
      service.setStopReason('a', 'end_turn');

      service.setViewedSession('a');
      expect(service.isChatLoading()).toBe(true);
      expect(service.currentStopReason()).toBe('end_turn');

      // Session B has its own untouched state.
      service.setViewedSession('b');
      expect(service.isChatLoading()).toBe(false);
      expect(service.currentStopReason()).toBeNull();
    });

    it('react to state created after the session is viewed', () => {
      service.setViewedSession('fresh');
      expect(service.isChatLoading()).toBe(false);

      service.setChatLoading('fresh', true);
      expect(service.isChatLoading()).toBe(true);
    });
  });

  describe('setChatLoading', () => {
    it('is isolated per session', () => {
      service.setChatLoading('a', true);
      service.setChatLoading('b', true);
      service.setChatLoading('b', false);

      expect(service.isSessionLoading('a')).toBe(true);
      expect(service.isSessionLoading('b')).toBe(false);
    });
  });

  describe('unread tracking', () => {
    it('flags a session unread when its response finishes while unviewed', () => {
      service.setViewedSession('a');

      service.setChatLoading('b', true);
      expect(service.isSessionUnread('b')).toBe(false);

      service.setChatLoading('b', false);
      expect(service.isSessionUnread('b')).toBe(true);
    });

    it('does not flag the currently-viewed session when its response finishes', () => {
      service.setViewedSession('a');
      service.setChatLoading('a', true);
      service.setChatLoading('a', false);
      expect(service.isSessionUnread('a')).toBe(false);
    });

    it('only flags on a true→false transition, not an idempotent stop', () => {
      service.setViewedSession('a');
      // No prior loading=true, so this is not a "finished" transition.
      service.setChatLoading('b', false);
      expect(service.isSessionUnread('b')).toBe(false);
    });

    it('clears the unread flag when the user opens the session', () => {
      service.setViewedSession('a');
      service.setChatLoading('b', true);
      service.setChatLoading('b', false);
      expect(service.isSessionUnread('b')).toBe(true);

      service.setViewedSession('b');
      expect(service.isSessionUnread('b')).toBe(false);
    });
  });

  describe('setLastTurnContinuable', () => {
    it('toggles the continuable flag per session', () => {
      service.setViewedSession('a');
      service.setLastTurnContinuable('a', true);
      expect(service.lastTurnContinuable()).toBe(true);

      // Another session's flag doesn't affect the viewed one.
      service.setLastTurnContinuable('b', false);
      expect(service.lastTurnContinuable()).toBe(true);

      service.setLastTurnContinuable('a', false);
      expect(service.lastTurnContinuable()).toBe(false);
    });
  });

  describe('setLastTurnInterrupted', () => {
    it('sets the flag + reason per session and clears the reason on false', () => {
      service.setViewedSession('a');
      service.setLastTurnInterrupted('a', true, 'user_stopped');
      expect(service.lastTurnInterrupted()).toBe(true);
      expect(service.lastTurnInterruptReason()).toBe('user_stopped');

      // Another session's flag doesn't affect the viewed one.
      service.setLastTurnInterrupted('b', true, 'connection_lost');
      expect(service.lastTurnInterrupted()).toBe(true);
      expect(service.lastTurnInterruptReason()).toBe('user_stopped');

      // Clearing drops both the flag and the reason.
      service.setLastTurnInterrupted('a', false);
      expect(service.lastTurnInterrupted()).toBe(false);
      expect(service.lastTurnInterruptReason()).toBeNull();
    });
  });

  describe('cost / context aggregates', () => {
    it('seeds, accumulates, and isolates per session', () => {
      service.setViewedSession('a');
      service.seedSessionAggregates('a', {
        totalCost: 1.5,
        lastContextTokens: 1000,
        contextWindow: 200000,
      });
      expect(service.costDollars()).toBe(1.5);
      expect(service.contextTokens()).toBe(1000);
      expect(service.contextWindowSize()).toBe(200000);

      service.addTurnCost('a', 0.5);
      expect(service.costDollars()).toBe(2);

      // A background session's turn cost must not leak into the viewed badge.
      service.addTurnCost('b', 10);
      expect(service.costDollars()).toBe(2);

      service.setContext('a', 3000, 200000);
      expect(service.contextTokens()).toBe(3000);
      expect(service.contextPct()).toBeCloseTo(1.5);
    });

    it('ignores non-finite or non-positive turn costs', () => {
      service.setViewedSession('a');
      service.addTurnCost('a', NaN);
      service.addTurnCost('a', -1);
      service.addTurnCost('a', 0);
      expect(service.costDollars()).toBe(0);
    });
  });

  describe('requestScrollToLastUser', () => {
    it('starts at 0 and increments the tick on each request', () => {
      expect(service.scrollToLastUserTick()).toBe(0);
      service.requestScrollToLastUser();
      expect(service.scrollToLastUserTick()).toBe(1);
      service.requestScrollToLastUser();
      expect(service.scrollToLastUserTick()).toBe(2);
    });
  });

  describe('abort controllers', () => {
    it('creates a fresh controller per session', () => {
      const a = service.createAbortController('a');
      const b = service.createAbortController('b');

      expect(a).toBeInstanceOf(AbortController);
      expect(a).not.toBe(b);
      expect(a.signal.aborted).toBe(false);
      expect(b.signal.aborted).toBe(false);
    });

    it('aborts the previous in-flight controller for the SAME session (double-submit guard)', () => {
      const first = service.createAbortController('a');
      const second = service.createAbortController('a');

      expect(first.signal.aborted).toBe(true);
      expect(second.signal.aborted).toBe(false);
    });

    it('abortRequest only aborts the target session', () => {
      const a = service.createAbortController('a');
      const b = service.createAbortController('b');

      service.abortRequest('b');

      expect(a.signal.aborted).toBe(false);
      expect(b.signal.aborted).toBe(true);
    });

    it('abortRequest is a no-op for sessions without an in-flight request', () => {
      expect(() => service.abortRequest('nope')).not.toThrow();
    });

    it('drops a session from streamingSessionIds once its controller is released', () => {
      // The bug this guards: a completed stream used to leave its controller
      // behind, so the session looked in-flight for the life of the tab and
      // the next page-hide marked its finished turn `navigated_away`.
      const a = service.createAbortController('a');
      service.createAbortController('b');

      expect(service.streamingSessionIds().sort()).toEqual(['a', 'b']);

      service.releaseAbortController('a', a);

      expect(service.streamingSessionIds()).toEqual(['b']);
      // Released, not aborted — the stream finished on its own.
      expect(a.signal.aborted).toBe(false);
    });

    it('releaseAbortController ignores a controller the session no longer owns', () => {
      // A superseded stream's late teardown must not clear the controller of
      // the stream that replaced it.
      const first = service.createAbortController('a');
      const second = service.createAbortController('a');

      service.releaseAbortController('a', first);

      expect(service.streamingSessionIds()).toEqual(['a']);
      expect(second.signal.aborted).toBe(false);
    });

    it('releaseAbortController is a no-op for an unknown session', () => {
      expect(() =>
        service.releaseAbortController('nope', new AbortController()),
      ).not.toThrow();
    });
  });
});
