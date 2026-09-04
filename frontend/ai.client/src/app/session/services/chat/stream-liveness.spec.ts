// stream-liveness.spec.ts
//
// A model call that produces nothing looks exactly like a hung one. In prod
// session 5f34d2b0 two turns went ~95 seconds with no output and the user
// abandoned both — the second one while the request was, as far as the
// telemetry shows, still in flight. The SPA holds the one fact that can tell
// those apart: when the last byte arrived.
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';

describe('StreamParserService - stream liveness', () => {
  let service: StreamParserService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [StreamParserService, ChatStateService, ErrorService, QuotaWarningService],
    });
    service = TestBed.inject(StreamParserService);
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('reports 0 before a stream has started', () => {
    expect(service.lastEventAtFor('never-started')()).toBe(0);
  });

  it('stamps the clock when a stream is reset', () => {
    const before = Date.now();
    service.reset('s1');
    expect(service.lastEventAtFor('s1')()).toBeGreaterThanOrEqual(before);
  });

  it('advances on each received event', () => {
    vi.useFakeTimers();
    service.reset('s1');
    const first = service.lastEventAtFor('s1')();

    vi.advanceTimersByTime(10_000);
    service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' });

    expect(service.lastEventAtFor('s1')()).toBe(first + 10_000);
  });

  it('advances on an event the state gate drops', () => {
    // Liveness is about the connection, not the payload. An event this stream
    // state ignores is still proof the server is talking.
    vi.useFakeTimers();
    service.reset('s1');
    service.parseEventSourceMessage('s1', 'done', {});
    const afterDone = service.lastEventAtFor('s1')();

    vi.advanceTimersByTime(5_000);
    service.parseEventSourceMessage('s1', 'metadata', { usage: {} });

    expect(service.lastEventAtFor('s1')()).toBe(afterDone + 5_000);
  });

  it('advances on a malformed event', () => {
    vi.useFakeTimers();
    service.reset('s1');
    const first = service.lastEventAtFor('s1')();

    vi.advanceTimersByTime(3_000);
    service.parseEventSourceMessage('s1', 'content_block_delta', { nonsense: true });

    expect(service.lastEventAtFor('s1')()).toBe(first + 3_000);
  });

  it('ignores events from a superseded stream', () => {
    // Otherwise a dead stream's late events would keep its replacement
    // looking alive, and the replacement's own stall would never surface.
    vi.useFakeTimers();
    service.reset('s1');
    const staleStreamId = service.getCurrentStreamId('s1');

    service.reset('s1'); // new stream for the same session
    const afterReset = service.lastEventAtFor('s1')();

    vi.advanceTimersByTime(20_000);
    service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' }, staleStreamId);

    expect(service.lastEventAtFor('s1')()).toBe(afterReset);
  });

  it('tracks each session independently', () => {
    vi.useFakeTimers();
    service.reset('s1');
    service.reset('s2');
    const s1Start = service.lastEventAtFor('s1')();

    vi.advanceTimersByTime(30_000);
    service.parseEventSourceMessage('s2', 'message_start', { role: 'assistant' });

    expect(service.lastEventAtFor('s1')()).toBe(s1Start);
    expect(service.lastEventAtFor('s2')()).toBe(s1Start + 30_000);
  });
});
