import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { AudioRecorderService } from '../voice/audio-recorder.service';
import { DictationChimeService } from './dictation-chime.service';

export type DictationStatus = 'idle' | 'connecting' | 'listening' | 'finishing';

/** Why a dictation ended with text: the user pressed Done, or it hit the time cap. */
export type DictationEndReason = 'stopped' | 'limit';

export interface DictationHandlers {
  /** The dictation finished; `text` is everything transcribed (may be empty). */
  onEnd(text: string, reason: DictationEndReason): void;
  /** The dictation failed. Whatever was transcribed is discarded. */
  onError(message: string): void;
}

/** `POST /dictation/ticket` 404s while the environment has dictation switched off. */
export class DictationUnavailableError extends Error {
  constructor() {
    super('Dictation is not available.');
    this.name = 'DictationUnavailableError';
  }
}

interface Segment {
  id: string;
  text: string;
  partial: boolean;
}

type ServerMessage =
  | { type: 'ready' }
  | { type: 'transcript'; id: string; text: string; partial: boolean; language: string | null }
  | { type: 'error'; code: string; message: string }
  | { type: 'done'; reason: DictationEndReason };

/** Bars in the live waveform: one per 100ms chunk, so ~2.4s of history. */
export const DICTATION_LEVEL_BARS = 24;

/** How long Done waits for the server's final segments before settling for what it has. */
const FINISH_TIMEOUT_MS = 12_000;

/** How long to wait for the upstream Transcribe stream to open. */
const READY_TIMEOUT_MS = 10_000;

/**
 * Chunks captured while the socket is still opening, sent once it is ready.
 * The mic starts at the same time as the socket so the first word is not
 * clipped; 50 chunks is 5s, far past any healthy connect.
 */
const MAX_PENDING_CHUNKS = 50;

/**
 * Speech-to-text into the composer via app-api's Transcribe proxy.
 *
 * Same transport shape as voice mode: a cookie-authenticated POST mints a
 * single-use ticket, then a same-origin WebSocket carries raw PCM up and
 * transcript segments down. Unlike voice mode nothing here reaches the model —
 * the composer decides what to do with the text.
 *
 * Segments are keyed by Transcribe's result id: a partial is revised in place
 * until its final arrives, so `transcript` is always "everything said so far",
 * with the in-progress tail included.
 */
@Injectable({ providedIn: 'root' })
export class DictationService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly recorder = inject(AudioRecorderService);
  private readonly chime = inject(DictationChimeService);

  private readonly _status = signal<DictationStatus>('idle');
  private readonly _segments = signal<Segment[]>([]);
  private readonly _levels = signal<number[]>(new Array(DICTATION_LEVEL_BARS).fill(0));
  private readonly _unavailable = signal(false);

  readonly status = this._status.asReadonly();
  readonly levels = this._levels.asReadonly();
  /** True once the server has said dictation is switched off; stays true for the tab. */
  readonly unavailable = this._unavailable.asReadonly();
  readonly isActive = computed(() => this._status() !== 'idle');
  readonly isSupported = this.recorder.isSupported;

  readonly transcript = computed(() =>
    this._segments()
      .map(segment => segment.text.trim())
      .filter(text => !!text)
      .join(' '),
  );

  private ws: WebSocket | null = null;
  private handlers: DictationHandlers | null = null;
  private pendingChunks: ArrayBuffer[] = [];
  private ready = false;
  private finishTimer: ReturnType<typeof setTimeout> | null = null;
  /** Settles an in-flight `openSocket` so a cancel does not wait out its timeout. */
  private abortConnect: (() => void) | null = null;

  /**
   * Open the mic and the transcription socket. Resolves once both are live;
   * rejects (after cleaning up) if either fails — including with
   * `DictationUnavailableError` when the feature is switched off.
   */
  async start(handlers: DictationHandlers): Promise<void> {
    if (this._status() !== 'idle') return;
    // Inside the user's click, before the first await: the start chime plays
    // once the socket is up, and a context created that late would be muted.
    this.chime.prime();
    this.reset();
    this.handlers = handlers;
    this.setStatus('connecting');

    try {
      const ticket = await this.issueTicket();
      // A cancel while the ticket was in flight ends the attempt quietly.
      if (this._status() !== 'connecting') return;

      this.recorder.onPcmChunk = pcm => this.onChunk(pcm);
      // Settled, not `all`: a rejection must not return while the other half
      // is still starting, or a mic that opens after teardown stays on.
      const [socket, mic] = await Promise.allSettled([
        this.openSocket(ticket),
        this.recorder.start(),
      ]);
      if (this._status() !== 'connecting') {
        // Cancelled mid-connect; teardown ran before the mic finished opening.
        void this.recorder.stop().catch(() => undefined);
        return;
      }
      if (mic.status === 'rejected') throw micError(mic.reason);
      if (socket.status === 'rejected') throw socket.reason;

      this.ready = true;
      for (const chunk of this.pendingChunks) this.ws?.send(chunk);
      this.pendingChunks = [];
      this.setStatus('listening');
    } catch (err) {
      this.teardown();
      throw err;
    }
  }

  /** The user is done: flush the tail, then `onEnd` fires with the final text. */
  finish(): void {
    const status = this._status();
    if (status === 'connecting') {
      // Nothing can have been transcribed yet.
      this.end('stopped');
      return;
    }
    if (status !== 'listening') return;
    this.setStatus('finishing');
    // Stopping the recorder flushes the last partial chunk through onPcmChunk.
    void this.recorder.stop().then(() => this.sendJson({ type: 'stop' }));
    this.finishTimer = setTimeout(() => this.end('stopped'), FINISH_TIMEOUT_MS);
  }

  /** Throw the dictation away. No handler fires. */
  cancel(): void {
    if (this._status() === 'idle') return;
    this.teardown();
  }

  private async issueTicket(): Promise<string> {
    try {
      const { ticket } = await firstValueFrom(
        this.http.post<{ ticket: string; expires_in: number }>(
          `${this.config.appApiUrl()}/dictation/ticket`,
          {},
        ),
      );
      return ticket;
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 404) {
        this._unavailable.set(true);
        throw new DictationUnavailableError();
      }
      throw new Error('Could not start dictation. Please try again.');
    }
  }

  private openSocket(ticket: string): Promise<void> {
    const httpUrl = this.config.appApiUrl();
    const wsBase = httpUrl.startsWith('http')
      ? httpUrl.replace(/^http/, 'ws')
      : `${window.location.origin.replace(/^http/, 'ws')}${httpUrl}`;

    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${wsBase}/dictation/stream?ticket=${encodeURIComponent(ticket)}`);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      let settled = false;
      const settle = (err?: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        this.abortConnect = null;
        if (err) reject(err);
        else resolve();
      };
      const timeout = setTimeout(
        () => settle(new Error('Dictation took too long to connect.')),
        READY_TIMEOUT_MS,
      );
      this.abortConnect = () => settle(new Error('Dictation was cancelled.'));

      ws.onmessage = (event: MessageEvent) => {
        const message = parse(event.data);
        if (!message) return;
        if (message.type === 'ready') {
          settle();
          return;
        }
        if (message.type === 'error' && !settled) {
          settle(new Error(message.message));
          return;
        }
        this.onServerMessage(message);
      };
      ws.onerror = () => settle(new Error('Could not connect to dictation.'));
      ws.onclose = () => {
        settle(new Error('Could not connect to dictation.'));
        this.onSocketClosed();
      };
    });
  }

  private onChunk(pcm: Int16Array): void {
    this.pushLevel(pcm);
    // Copy out of the recorder's buffer: `send` may run after it is reused.
    const frame = pcm.slice().buffer;
    if (this.ready) {
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(frame);
      return;
    }
    if (this.pendingChunks.length < MAX_PENDING_CHUNKS) this.pendingChunks.push(frame);
  }

  private onServerMessage(message: ServerMessage): void {
    switch (message.type) {
      case 'transcript':
        this.upsertSegment({ id: message.id, text: message.text, partial: message.partial });
        return;
      case 'done':
        this.end(message.reason);
        return;
      case 'error':
        this.fail(message.message);
        return;
    }
  }

  /** The socket went away without a `done` or `error` frame. */
  private onSocketClosed(): void {
    const status = this._status();
    if (status === 'finishing') {
      // The tail may be missing, but what arrived is still the user's words.
      this.end('stopped');
    } else if (status === 'listening') {
      this.fail('Dictation was interrupted.');
    }
  }

  private upsertSegment(segment: Segment): void {
    this._segments.update(segments => {
      const index = segments.findIndex(existing => existing.id === segment.id);
      if (index === -1) return [...segments, segment];
      const next = [...segments];
      next[index] = segment;
      return next;
    });
  }

  private pushLevel(pcm: Int16Array): void {
    let sum = 0;
    for (let i = 0; i < pcm.length; i++) sum += pcm[i] * pcm[i];
    const rms = pcm.length ? Math.sqrt(sum / pcm.length) / 0x8000 : 0;
    // Speech RMS sits around 0.02–0.2; a square-root curve lifts quiet
    // speech into a visible bar without pinning loud speech at the top.
    const level = Math.min(1, Math.sqrt(rms * 4));
    this._levels.update(levels => [...levels.slice(1), level]);
  }

  private end(reason: DictationEndReason): void {
    const handlers = this.handlers;
    const text = this.transcript();
    this.teardown();
    handlers?.onEnd(text, reason);
  }

  private fail(message: string): void {
    const handlers = this.handlers;
    this.teardown();
    handlers?.onError(message);
  }

  private sendJson(payload: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
  }

  private teardown(): void {
    if (this.finishTimer) {
      clearTimeout(this.finishTimer);
      this.finishTimer = null;
    }
    this.abortConnect?.();
    this.recorder.onPcmChunk = null;
    void this.recorder.stop().catch(() => undefined);
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        // Already closed.
      }
    }
    this.handlers = null;
    this.reset();
    this.setStatus('idle');
  }

  /**
   * Every status change goes through here so the chimes track the mic, not
   * the buttons: rising on the way into `listening` (the first moment speech
   * is captured), falling on any way out of it — Done, Cancel, the time
   * limit, or a dropped socket. An attempt that never reached `listening`
   * made no start sound, so it makes no stop sound either.
   */
  private setStatus(next: DictationStatus): void {
    const previous = this._status();
    if (previous === next) return;
    this._status.set(next);
    if (next === 'listening') {
      this.chime.play('start');
    } else if (previous === 'listening') {
      this.chime.play('stop');
    }
  }

  private reset(): void {
    this.ready = false;
    this.pendingChunks = [];
    this._segments.set([]);
    this._levels.set(new Array(DICTATION_LEVEL_BARS).fill(0));
  }
}

function micError(reason: unknown): Error {
  if (reason instanceof DOMException && reason.name === 'NotAllowedError') {
    return new Error('Allow microphone access in your browser to dictate.');
  }
  if (reason instanceof DOMException && reason.name === 'NotFoundError') {
    return new Error('No microphone was found.');
  }
  return reason instanceof Error ? reason : new Error('Could not start the microphone.');
}

function parse(data: unknown): ServerMessage | null {
  if (typeof data !== 'string') return null;
  try {
    const parsed = JSON.parse(data) as ServerMessage;
    return parsed && typeof parsed === 'object' && 'type' in parsed ? parsed : null;
  } catch {
    return null;
  }
}
