import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { AudioRecorderService } from '../voice/audio-recorder.service';
import {
  DictationService,
  DictationUnavailableError,
  type DictationEndReason,
} from './dictation.service';

/** A WebSocket the test drives by hand; the latest instance is `FakeSocket.last`. */
class FakeSocket {
  static readonly OPEN = 1;
  static last: FakeSocket | null = null;
  readonly url: string;
  readyState = FakeSocket.OPEN;
  binaryType = 'blob';
  sent: (string | ArrayBuffer)[] = [];
  closed = false;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeSocket.last = this;
  }

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
  }

  serverSends(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent);
  }

  get audioFrames(): ArrayBuffer[] {
    return this.sent.filter((frame): frame is ArrayBuffer => frame instanceof ArrayBuffer);
  }

  get jsonFrames(): unknown[] {
    return this.sent.filter(frame => typeof frame === 'string').map(frame => JSON.parse(frame as string));
  }
}

class RecorderStub {
  readonly isSupported = signal(true);
  onPcmChunk: ((pcm: Int16Array) => void) | null = null;
  recording = false;
  startImpl: () => Promise<void> = async () => undefined;
  readonly start = vi.fn(async () => {
    await this.startImpl();
    this.recording = true;
  });
  readonly stop = vi.fn(async () => {
    this.recording = false;
  });

  chunk(value = 1000): void {
    this.onPcmChunk?.(new Int16Array(1600).fill(value));
  }
}

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

describe('DictationService', () => {
  let service: DictationService;
  let http: HttpTestingController;
  let recorder: RecorderStub;
  let onEnd: Mock<(text: string, reason: DictationEndReason) => void>;
  let onError: Mock<(message: string) => void>;
  const OriginalWebSocket = globalThis.WebSocket;

  beforeEach(() => {
    FakeSocket.last = null;
    globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket;
    recorder = new RecorderStub();
    onEnd = vi.fn();
    onError = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: AudioRecorderService, useValue: recorder },
      ],
    });
    service = TestBed.inject(DictationService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    globalThis.WebSocket = OriginalWebSocket;
    vi.useRealTimers();
  });

  /** Drive `start()` through the ticket and the socket's `ready`. */
  async function startListening(): Promise<FakeSocket> {
    const started = service.start({ onEnd, onError });
    http.expectOne('http://localhost:8000/dictation/ticket').flush({ ticket: 'tkt', expires_in: 60 });
    await flush();
    const socket = FakeSocket.last!;
    socket.serverSends({ type: 'ready' });
    await started;
    return socket;
  }

  it('opens the ticketed socket and streams raw PCM once ready', async () => {
    const socket = await startListening();
    expect(socket.url).toBe('ws://localhost:8000/dictation/stream?ticket=tkt');
    expect(socket.binaryType).toBe('arraybuffer');
    expect(service.status()).toBe('listening');

    recorder.chunk();
    expect(socket.audioFrames).toHaveLength(1);
    expect(socket.audioFrames[0].byteLength).toBe(3200);
  });

  it('sends audio captured before the socket was ready, in order, once it is', async () => {
    const started = service.start({ onEnd, onError });
    http.expectOne('http://localhost:8000/dictation/ticket').flush({ ticket: 'tkt', expires_in: 60 });
    await flush();
    const socket = FakeSocket.last!;
    recorder.chunk(1);
    recorder.chunk(2);
    expect(socket.audioFrames).toHaveLength(0);

    socket.serverSends({ type: 'ready' });
    await started;
    expect(socket.audioFrames.map(frame => new Int16Array(frame)[0])).toEqual([1, 2]);
  });

  it('revises partials in place and joins segments into one transcript', async () => {
    const socket = await startListening();
    socket.serverSends({ type: 'transcript', id: 'a', text: 'Hello', partial: true, language: null });
    socket.serverSends({ type: 'transcript', id: 'a', text: 'Hello there.', partial: false, language: null });
    socket.serverSends({ type: 'transcript', id: 'b', text: 'How', partial: true, language: null });
    expect(service.transcript()).toBe('Hello there. How');
  });

  it('Done stops the mic, sends stop, and hands over the text on the server done', async () => {
    const socket = await startListening();
    socket.serverSends({ type: 'transcript', id: 'a', text: 'Fix the bug.', partial: true, language: 'en-US' });

    service.finish();
    expect(service.status()).toBe('finishing');
    await flush();
    expect(recorder.stop).toHaveBeenCalled();
    expect(socket.jsonFrames).toEqual([{ type: 'stop' }]);

    socket.serverSends({ type: 'transcript', id: 'a', text: 'Fix the bug.', partial: false, language: 'en-US' });
    socket.serverSends({ type: 'done', reason: 'stopped' });
    expect(onEnd).toHaveBeenCalledWith('Fix the bug.', 'stopped');
    expect(service.status()).toBe('idle');
    expect(socket.closed).toBe(true);
  });

  it('Done settles for what it has if the server never finishes', async () => {
    vi.useFakeTimers();
    const socket = await startListening();
    socket.serverSends({ type: 'transcript', id: 'a', text: 'Partial words', partial: true, language: null });
    service.finish();
    await vi.advanceTimersByTimeAsync(12_000);
    expect(onEnd).toHaveBeenCalledWith('Partial words', 'stopped');
  });

  it('a socket drop while listening is an error; while finishing it keeps the text', async () => {
    let socket = await startListening();
    socket.onclose?.();
    expect(onError).toHaveBeenCalledWith('Dictation was interrupted.');
    expect(service.status()).toBe('idle');

    socket = await startListening();
    socket.serverSends({ type: 'transcript', id: 'a', text: 'kept', partial: true, language: null });
    service.finish();
    socket.onclose?.();
    expect(onEnd).toHaveBeenCalledWith('kept', 'stopped');
  });

  it('a server error frame fails the dictation with its message', async () => {
    const socket = await startListening();
    socket.serverSends({ type: 'error', code: 'busy', message: 'Too many people are dictating right now.' });
    expect(onError).toHaveBeenCalledWith('Too many people are dictating right now.');
    expect(onEnd).not.toHaveBeenCalled();
  });

  it('cancel tears down without calling either handler', async () => {
    const socket = await startListening();
    service.cancel();
    expect(socket.closed).toBe(true);
    expect(recorder.onPcmChunk).toBeNull();
    expect(service.status()).toBe('idle');
    expect(onEnd).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it('marks itself unavailable on a 404 ticket', async () => {
    const started = service.start({ onEnd, onError });
    http
      .expectOne('http://localhost:8000/dictation/ticket')
      .flush('Not Found', { status: 404, statusText: 'Not Found' });
    await expect(started).rejects.toBeInstanceOf(DictationUnavailableError);
    expect(service.unavailable()).toBe(true);
    expect(service.status()).toBe('idle');
  });

  it('a blocked microphone rejects with a readable message and closes the socket', async () => {
    recorder.startImpl = async () => {
      throw new DOMException('denied', 'NotAllowedError');
    };
    const started = service.start({ onEnd, onError });
    http.expectOne('http://localhost:8000/dictation/ticket').flush({ ticket: 'tkt', expires_in: 60 });
    await flush();
    FakeSocket.last!.serverSends({ type: 'ready' });
    await expect(started).rejects.toThrow('Allow microphone access in your browser to dictate.');
    expect(FakeSocket.last!.closed).toBe(true);
  });

  it('a cancel while connecting ends quietly and never leaves the mic on', async () => {
    let releaseMic: () => void = () => undefined;
    recorder.startImpl = () => new Promise<void>(resolve => (releaseMic = resolve));
    const started = service.start({ onEnd, onError });
    http.expectOne('http://localhost:8000/dictation/ticket').flush({ ticket: 'tkt', expires_in: 60 });
    await flush();

    service.cancel();
    releaseMic();
    await expect(started).resolves.toBeUndefined();
    await flush();
    expect(recorder.recording).toBe(false);
    expect(service.status()).toBe('idle');
  });

  it('tracks a rolling level per chunk for the waveform', async () => {
    await startListening();
    recorder.chunk(0);
    recorder.chunk(8000);
    const levels = service.levels();
    expect(levels[levels.length - 2]).toBe(0);
    expect(levels[levels.length - 1]).toBeGreaterThan(0.5);
  });
});
