import { Injectable, signal, computed, inject, OnDestroy } from '@angular/core';
import { AuthService } from '../../../auth/auth.service';
import { ConfigService } from '../../../services/config.service';
import { AudioRecorderService } from './audio-recorder.service';
import { AudioPlayerService } from './audio-player.service';

export type VoiceStatus = 'idle' | 'connecting' | 'listening' | 'speaking';

/** Idle timeout — auto-disconnect after 60s of silence */
const IDLE_TIMEOUT_MS = 60_000;

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
  private readonly _currentTranscript = signal('');
  private readonly _isConnected = signal(false);
  private readonly _lastTranscriptRole = signal<'user' | 'assistant'>('assistant');

  readonly status = this._status.asReadonly();
  readonly currentTranscript = this._currentTranscript.asReadonly();
  readonly lastTranscriptRole = this._lastTranscriptRole.asReadonly();
  readonly isConnected = this._isConnected.asReadonly();
  readonly isVoiceActive = computed(() => this._status() !== 'idle');

  /** Emitted when a complete assistant response is finalized */
  onResponseComplete: ((transcript: string) => void) | null = null;

  // --- Internals ---
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Connect to the voice endpoint and start recording.
   */
  async connect(sessionId: string): Promise<void> {
    if (this._isConnected()) return;

    this.sessionId = sessionId;
    this._status.set('connecting');
    this._currentTranscript.set('');

    try {
      const token = this.authService.getAccessToken();
      if (!token) {
        throw new Error('No authentication token available');
      }

      // Build WebSocket URL from inference API URL
      const httpUrl = this.configService.inferenceApiUrl();
      const wsUrl = httpUrl.replace(/^http/, 'ws');
      const url = `${wsUrl}/voice/stream?session_id=${encodeURIComponent(sessionId)}&token=${encodeURIComponent(token)}`;

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
      }, 10_000);

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

      this.ws.onclose = () => {
        if (this._isConnected()) {
          // Unexpected close — clean up
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

      case 'bidi_transcript_stream':
        // Accumulate transcript text
        if (data['role']) {
          this._lastTranscriptRole.set(data['role'] as 'user' | 'assistant');
        }
        if (data['delta']) {
          this._currentTranscript.update(t => t + (data['delta'] as string));
        } else if (data['current_transcript']) {
          this._currentTranscript.set(data['current_transcript'] as string);
        }
        this.resetIdleTimer();
        break;

      case 'bidi_response_start':
        this._status.set('speaking');
        this._currentTranscript.set('');
        break;

      case 'bidi_response_complete':
        // Agent finished speaking
        this._status.set('listening');
        const transcript = this._currentTranscript();
        if (transcript) {
          this.onResponseComplete?.(transcript);
        }
        this._currentTranscript.set('');
        break;

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
