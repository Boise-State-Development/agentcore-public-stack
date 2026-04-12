import { Injectable, signal, computed, inject, OnDestroy } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { AuthService } from '../../../auth/auth.service';
import { ConfigService } from '../../../services/config.service';
import { AudioRecorderService } from './audio-recorder.service';
import { AudioPlayerService } from './audio-player.service';
import {
  IDLE_TIMEOUT_MS,
  WS_CONNECT_TIMEOUT_MS,
  MAX_TRANSCRIPT_ENTRIES,
  WS_CLOSE_REASONS,
  REVEAL_CHARS_PER_TICK,
  REVEAL_TICK_MS,
  REVEAL_FLUSH_CHARS_PER_TICK,
} from './voice.config';

export type VoiceStatus = 'idle' | 'connecting' | 'listening' | 'speaking';

export interface VoiceTranscriptEntry {
  role: 'user' | 'assistant';
  text: string;
  timestamp: number;
}

/**
 * Voice chat orchestration service.
 *
 * Coordinates WebSocket connection to /voice/stream, audio recording,
 * audio playback, and state management for the voice UI.
 *
 * Lifecycle:
 *   1. connect(sessionId) → opens WebSocket, sends config, starts mic
 *   2. Audio chunks sent as bidi_audio_input
 *   3. Server events received: bidi_audio_stream → playback,
 *      bidi_transcript_stream → transcript signal
 *   4. disconnect() → stops mic, closes WebSocket, clears audio
 */
@Injectable({ providedIn: 'root' })
export class VoiceChatService implements OnDestroy {
  private readonly authService = inject(AuthService);
  private readonly configService = inject(ConfigService);
  private readonly recorder = inject(AudioRecorderService);
  private readonly player = inject(AudioPlayerService);

  // --- State signals ---
  private readonly _status = signal<VoiceStatus>('idle');
  private readonly _isConnected = signal(false);
  private readonly _transcriptEntries = signal<VoiceTranscriptEntry[]>([]);

  /**
   * Buffered reveal transcript system.
   *
   * _bufferedTranscript: raw text from WebSocket — grows instantly as deltas arrive.
   * _revealedIndex: how many characters the UI is allowed to see.
   * agentTranscript: computed slice — what the template actually renders.
   *
   * A reveal timer advances _revealedIndex at speaking pace, creating a
   * typewriter effect that stays roughly in sync with the audio.
   */
  private readonly _bufferedTranscript = signal('');
  private readonly _revealedIndex = signal(0);
  private _revealCharsPerTick = REVEAL_CHARS_PER_TICK;
  private _revealTimer: ReturnType<typeof setInterval> | null = null;

  /**
   * Per-turn tracking for transcript entries.
   *
   * _currentTurnStart: index into _bufferedTranscript where the current
   *   assistant turn began, so we can extract just this turn's text.
   * _userTranscript: accumulates user speech text between assistant turns,
   *   saved to transcriptEntries when the assistant starts responding.
   */
  private _currentTurnStart = 0;
  private _userTranscript = '';

  readonly status = this._status.asReadonly();
  readonly agentTranscript = computed(() =>
    this._bufferedTranscript().slice(0, this._revealedIndex())
  );
  readonly isConnected = this._isConnected.asReadonly();
  readonly isVoiceActive = computed(() => this._status() !== 'idle');
  readonly transcriptEntries = this._transcriptEntries.asReadonly();

  // --- Internals ---
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Connect to the voice endpoint and start recording.
   * If no sessionId is provided, one is auto-generated.
   */
  async connect(sessionId?: string): Promise<void> {
    if (this._isConnected()) return;

    this.sessionId = sessionId || uuidv4();
    this._status.set('connecting');
    this._bufferedTranscript.set('');
    this._revealedIndex.set(0);
    this._currentTurnStart = 0;
    this._userTranscript = '';
    this.stopRevealTimer();
    this._transcriptEntries.set([]);

    try {
      const token = this.authService.getAccessToken();
      if (!token) {
        throw new Error('No authentication token available');
      }

      // Build WebSocket URL from inference API URL
      const httpUrl = this.configService.inferenceApiUrl();
      const wsUrl = httpUrl.replace(/^http/, 'ws');
      const url = `${wsUrl}/voice/stream?session_id=${encodeURIComponent(this.sessionId!)}&token=${encodeURIComponent(token)}`;

      await this.openWebSocket(url, token);
      await this.recorder.start();

      // Wire audio chunks to WebSocket
      this.recorder.onAudioChunk = (base64, sampleRate) => {
        this.sendMessage({
          type: 'bidi_audio_input',
          audio: base64,
          sample_rate: sampleRate,
        });
      };

      this._status.set('listening');
      this._isConnected.set(true);
      this.resetIdleTimer();
    } catch (err) {
      this.cleanupAll();
      this._status.set('idle');
      throw err;
    }
  }

  /** Disconnect from voice session and release all resources. */
  async disconnect(): Promise<void> {
    if (!this._isConnected()) return;
    this.sendMessage({ type: 'stop' });
    this.cleanupAll();
    this._status.set('idle');
    this._isConnected.set(false);
  }

  /** Get the current voice session ID (auto-generated or passed to connect). */
  getSessionId(): string | null {
    return this.sessionId;
  }

  /** Send a text message (fallback when mic not available). */
  sendText(text: string): void {
    if (!this._isConnected()) return;
    this.sendMessage({ type: 'bidi_text_input', text });
    this.resetIdleTimer();
  }

  ngOnDestroy(): void {
    this.cleanupAll();
  }

  // --- WebSocket ---

  private openWebSocket(url: string, token: string): Promise<void> {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(url);

      const timeout = setTimeout(() => {
        reject(new Error('WebSocket connection timeout'));
        this.ws?.close();
      }, WS_CONNECT_TIMEOUT_MS);

      this.ws.onopen = () => {
        clearTimeout(timeout);
        // Send config message as first frame
        this.sendMessage({
          type: 'config',
          session_id: this.sessionId,
          auth_token: token,
        });
        resolve();
      };

      this.ws.onmessage = (event: MessageEvent) => {
        this.handleServerMessage(event);
      };

      this.ws.onerror = () => {
        clearTimeout(timeout);
        reject(new Error('WebSocket connection failed'));
      };

      this.ws.onclose = (event: CloseEvent) => {
        if (this._isConnected()) {
          // Unexpected close — log reason and clean up
          const reason = WS_CLOSE_REASONS[event.code] || `Disconnected (code ${event.code})`;
          console.warn(`Voice WebSocket closed: ${reason}`);
          this.cleanupAll();
          this._status.set('idle');
          this._isConnected.set(false);
        }
      };
    });
  }

  private sendMessage(msg: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  private handleServerMessage(event: MessageEvent): void {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(event.data as string);
    } catch {
      return;
    }

    const type = data['type'] as string;

    switch (type) {
      case 'bidi_connection_start':
        // Connection confirmed
        break;

      case 'bidi_audio_stream':
        // Play audio from agent
        this._status.set('speaking');
        if (data['audio']) {
          this.player.play(
            data['audio'] as string,
            (data['sample_rate'] as number) || 16000
          );
        }
        this.resetIdleTimer();
        break;

      case 'bidi_transcript_stream': {
        const role = data['role'] as string | undefined;

        let deltaText = '';
        if (data['delta']) {
          const delta = data['delta'];
          deltaText = typeof delta === 'object' && delta !== null
            ? (delta as Record<string, unknown>)['text'] as string
            : delta as string;
        } else if (data['current_transcript']) {
          deltaText = data['current_transcript'] as string;
        }

        if (deltaText) {
          if (role === 'assistant') {
            // Buffer for reveal-based display
            this._bufferedTranscript.update(t => t + deltaText);
          } else {
            // Accumulate user speech for transcript entries
            this._userTranscript += deltaText;
          }
        }

        this.resetIdleTimer();
        break;
      }

      case 'bidi_response_start':
        // Save accumulated user speech as a transcript entry
        if (this._userTranscript.trim()) {
          this.appendTranscriptEntry({
            role: 'user',
            text: this._userTranscript.trim(),
            timestamp: Date.now(),
          });
          this._userTranscript = '';
        }

        // New assistant turn — add separator if there's prior text, start reveal
        if (this._bufferedTranscript()) {
          this._bufferedTranscript.update(t => t + '\n\n');
          this._revealedIndex.set(this._bufferedTranscript().length);
        }
        this._currentTurnStart = this._bufferedTranscript().length;
        this._revealCharsPerTick = REVEAL_CHARS_PER_TICK;
        this.startRevealTimer();
        this._status.set('speaking');
        break;

      case 'bidi_response_complete': {
        // Extract only this turn's assistant text for the transcript entry
        const turnText = this._bufferedTranscript().slice(this._currentTurnStart).trim();
        if (turnText) {
          this.appendTranscriptEntry({
            role: 'assistant',
            text: turnText,
            timestamp: Date.now(),
          });
        }

        // Speed up reveal to flush remaining buffered text
        this._revealCharsPerTick = REVEAL_FLUSH_CHARS_PER_TICK;
        this._status.set('listening');
        break;
      }

      case 'bidi_interruption':
        // User interrupted — stop playback
        this.player.clear();
        this._status.set('listening');
        break;

      case 'bidi_usage':
        // Token usage stats — could emit for metadata display
        break;

      case 'bidi_error':
        console.error('Voice error:', data['message']);
        break;

      case 'bidi_connection_close':
        this.cleanupAll();
        this._status.set('idle');
        this._isConnected.set(false);
        break;

      case 'pong':
        // Keepalive response
        break;
    }
  }

  // --- Transcript reveal ---

  private startRevealTimer(): void {
    if (this._revealTimer) return; // already running
    this._revealTimer = setInterval(() => {
      const bufferedLen = this._bufferedTranscript().length;
      const current = this._revealedIndex();
      if (current >= bufferedLen) {
        // Caught up — if not speaking anymore, stop the timer
        if (this._status() !== 'speaking') {
          this.stopRevealTimer();
        }
        return;
      }
      // Advance by configured chars per tick, snapping to word boundaries
      const target = Math.min(current + this._revealCharsPerTick, bufferedLen);
      // Find the next space or end-of-buffer to avoid splitting mid-word
      const buf = this._bufferedTranscript();
      let end = target;
      if (end < bufferedLen && buf[end] !== ' ' && buf[end] !== '\n') {
        const nextSpace = buf.indexOf(' ', end);
        const nextNewline = buf.indexOf('\n', end);
        const nearest = nextSpace === -1 ? nextNewline
          : nextNewline === -1 ? nextSpace
          : Math.min(nextSpace, nextNewline);
        end = nearest === -1 ? bufferedLen : nearest + 1;
      }
      this._revealedIndex.set(end);
    }, REVEAL_TICK_MS);
  }

  private stopRevealTimer(): void {
    if (this._revealTimer) {
      clearInterval(this._revealTimer);
      this._revealTimer = null;
    }
  }

  // --- Transcript management ---

  /** Append an entry, evicting the oldest if at capacity. */
  private appendTranscriptEntry(entry: VoiceTranscriptEntry): void {
    this._transcriptEntries.update(entries => {
      const updated = [...entries, entry];
      return updated.length > MAX_TRANSCRIPT_ENTRIES
        ? updated.slice(updated.length - MAX_TRANSCRIPT_ENTRIES)
        : updated;
    });
  }

  // --- Idle timeout ---

  private resetIdleTimer(): void {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
    }
    this.idleTimer = setTimeout(() => {
      if (this._isConnected()) {
        console.info('Voice idle timeout — disconnecting');
        this.disconnect();
      }
    }, IDLE_TIMEOUT_MS);
  }

  // --- Cleanup ---

  private cleanupAll(): void {
    this.stopRevealTimer();

    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }

    this.recorder.onAudioChunk = null;
    this.recorder.stop().catch(() => {});
    this.player.clear();

    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // Already closed
      }
      this.ws = null;
    }
  }
}
