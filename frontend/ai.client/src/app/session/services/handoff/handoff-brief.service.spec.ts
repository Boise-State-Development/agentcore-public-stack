import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { FetchEventSourceInit } from '@microsoft/fetch-event-source';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ConfigService } from '../../../services/config.service';
import { SessionAffinityService } from '../session/session-affinity.service';
import { HANDOFF_BRIEF_SSE, HandoffBriefHandlers, HandoffBriefService } from './handoff-brief.service';

const API = 'https://api.test';

interface FakeStream {
  url: string;
  init: FetchEventSourceInit;
  body: Record<string, unknown>;
  /** The server closed the stream. */
  close(): void;
  emit(event: string, data: unknown): void;
  open(status?: number): Promise<void>;
}

/** A stand-in for fetch-event-source: the spec drives frames and the close. */
function fakeSse() {
  const streams: FakeStream[] = [];
  const fn = vi.fn((url: string, init: FetchEventSourceInit) => {
    return new Promise<void>((resolve, reject) => {
      // fetch-event-source resolves silently on abort.
      init.signal?.addEventListener('abort', () => resolve());
      streams.push({
        url,
        init,
        body: JSON.parse(init.body as string),
        close: () => resolve(),
        emit: (event, data) => init.onmessage?.({ id: '', event, data: JSON.stringify(data) }),
        open: async (status = 200) => {
          const response = new Response(null, {
            status,
            headers: status === 200 ? { 'content-type': 'text/event-stream' } : {},
          });
          try {
            await init.onopen?.(response);
          } catch (error) {
            reject(error);
          }
        },
      });
    });
  });
  return { fn, streams };
}

function handlers() {
  return {
    onText: vi.fn(),
    onFallback: vi.fn(),
    onDone: vi.fn(),
  } satisfies HandoffBriefHandlers;
}

describe('HandoffBriefService', () => {
  let service: HandoffBriefService;
  let http: HttpTestingController;
  let sse: ReturnType<typeof fakeSse>;

  beforeEach(() => {
    sse = fakeSse();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: BffSessionService, useValue: { csrfHeaders: () => ({ 'X-CSRF-Token': 't' }) } },
        { provide: SessionAffinityService, useValue: { affinityFor: () => null } },
        { provide: HANDOFF_BRIEF_SSE, useValue: sse.fn },
      ],
    });
    service = TestBed.inject(HandoffBriefService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    vi.useRealTimers();
  });

  it('posts the turn body to /chat/stream and streams text, then done with the hash', async () => {
    const h = handlers();
    const done = service.stream('src', 'h1', { session_id: 'src', handoff_brief: { handoffId: 'h1' } }, h);
    const stream = sse.streams[0];
    expect(stream.url).toBe(`${API}/chat/stream`);
    expect(stream.body).toMatchObject({ session_id: 'src', handoff_brief: { handoffId: 'h1' } });
    expect(service.isRunning('src')).toBe(true);

    await stream.open();
    stream.emit('content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'Goal: ' } });
    stream.emit('content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'a rubric' } });
    stream.emit('done', { type: 'done', handoffId: 'h1', briefHash: 'bh', cacheStatus: 'hit' });
    stream.close();
    await done;

    expect(h.onText.mock.calls.map((c) => c[0])).toEqual(['Goal: ', 'a rubric']);
    expect(h.onDone).toHaveBeenCalledWith({ briefHash: 'bh', cacheStatus: 'hit' });
    expect(h.onFallback).not.toHaveBeenCalled();
    expect(service.isRunning('src')).toBe(false);
  });

  it('reports a fallback frame once and ignores the done that follows it', async () => {
    const h = handlers();
    const done = service.stream('src', 'h1', {}, h);
    const stream = sse.streams[0];
    await stream.open();
    stream.emit('handoff_brief_fallback', { type: 'handoff_brief_fallback', handoffId: 'h1', reason: 'short_source' });
    stream.emit('done', { type: 'done', handoffId: 'h1' });
    stream.close();
    await done;

    expect(h.onFallback).toHaveBeenCalledTimes(1);
    expect(h.onFallback).toHaveBeenCalledWith('short_source');
    expect(h.onDone).not.toHaveBeenCalled();
  });

  it('a done without a hash is no brief', async () => {
    const h = handlers();
    const done = service.stream('src', 'h1', {}, h);
    await sse.streams[0].open();
    sse.streams[0].emit('done', { type: 'done', handoffId: 'h1' });
    sse.streams[0].close();
    await done;
    expect(h.onFallback).toHaveBeenCalledWith('error');
  });

  it('a 409 before streaming falls back as busy, and never retries', async () => {
    const h = handlers();
    const done = service.stream('src', 'h1', {}, h);
    await sse.streams[0].open(409);
    await done;
    expect(h.onFallback).toHaveBeenCalledWith('busy');
    expect(sse.fn).toHaveBeenCalledTimes(1);
  });

  it('preempt arms the cancel-only interrupt and resolves once the brief stream has closed', async () => {
    const h = handlers();
    const done = service.stream('src', 'h1', {}, h);
    const stream = sse.streams[0];
    await stream.open();

    let preempted = false;
    const preempt = service.preempt('src').then(() => (preempted = true));
    const interrupt = http.expectOne(`${API}/sessions/src/interrupt`);
    expect(interrupt.request.method).toBe('POST');
    expect(interrupt.request.body).toEqual({ reason: 'handoff_brief_preempted' });
    interrupt.flush(null, { status: 204, statusText: 'No Content' });

    await Promise.resolve();
    expect(preempted).toBe(false);

    stream.emit('handoff_brief_fallback', { type: 'handoff_brief_fallback', handoffId: 'h1', reason: 'preempted' });
    stream.emit('done', { type: 'done', handoffId: 'h1' });
    stream.close();
    await preempt;
    await done;

    expect(preempted).toBe(true);
    expect(h.onFallback).toHaveBeenCalledWith('preempted');
    expect(service.isRunning('src')).toBe(false);
  });

  it('preempt is a no-op with no brief in flight', async () => {
    await service.preempt('src');
    http.expectNone(`${API}/sessions/src/interrupt`);
  });

  it('a preempt that never sees the stream close cuts the transport after its bound', async () => {
    vi.useFakeTimers();
    const h = handlers();
    const done = service.stream('src', 'h1', {}, h);
    await sse.streams[0].open();

    const preempt = service.preempt('src');
    await vi.advanceTimersByTimeAsync(9000);
    await preempt;
    await done;

    // Re-armed while waiting: a cancel armed before the brief took the lease is a no-op.
    const interrupts = http.match(`${API}/sessions/src/interrupt`);
    expect(interrupts.length).toBeGreaterThan(1);
    expect(sse.streams[0].init.signal?.aborted).toBe(true);
    expect(h.onFallback).toHaveBeenCalledWith('preempted');
  });
});
