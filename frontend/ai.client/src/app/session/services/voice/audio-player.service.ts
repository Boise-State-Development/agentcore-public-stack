import { Injectable, signal } from '@angular/core';
import { base64ToSamples } from './pcm-utils';

/**
 * Audio playback service for Nova Sonic voice responses.
 *
 * Plays base64-encoded PCM audio chunks in sequence with gapless
 * scheduling. Supports interruption (clear all scheduled audio)
 * and volume control.
 */
@Injectable({ providedIn: 'root' })
export class AudioPlayerService {
  private readonly _isPlaying = signal(false);
  readonly isPlaying = this._isPlaying.asReadonly();

  private audioContext: AudioContext | null = null;
  private gainNode: GainNode | null = null;
  private scheduledTime = 0;
  private activeSources: AudioBufferSourceNode[] = [];

  /**
   * Play a base64-encoded PCM audio chunk.
   * Chunks are scheduled sequentially for gapless playback.
   */
  play(base64Pcm: string, sampleRate: number = 16000): void {
    if (!base64Pcm) return;

    this.ensureContext(sampleRate);
    if (!this.audioContext || !this.gainNode) return;

    const samples = base64ToSamples(base64Pcm);
    const buffer = this.audioContext.createBuffer(1, samples.length, sampleRate);
    buffer.getChannelData(0).set(samples);

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gainNode);

    // Schedule at the end of the current queue (gapless)
    const now = this.audioContext.currentTime;
    const startTime = Math.max(now, this.scheduledTime);
    source.start(startTime);
    this.scheduledTime = startTime + buffer.duration;

    // Track for interruption
    this.activeSources.push(source);
    source.onended = () => {
      const idx = this.activeSources.indexOf(source);
      if (idx >= 0) this.activeSources.splice(idx, 1);
      this._isPlaying.set(this.activeSources.length > 0);
    };

    this._isPlaying.set(true);
  }

  /**
   * Stop all scheduled and playing audio immediately.
   * Used when the user interrupts the agent.
   */
  clear(): void {
    for (const source of this.activeSources) {
      try {
        source.stop();
      } catch {
        // Already stopped
      }
    }
    this.activeSources = [];
    this.scheduledTime = 0;
    this._isPlaying.set(false);
  }

  /** Set playback volume (0.0 to 1.0). */
  setVolume(volume: number): void {
    if (this.gainNode) {
      this.gainNode.gain.value = Math.max(0, Math.min(1, volume));
    }
  }

  /** Release audio resources. */
  dispose(): void {
    this.clear();
    if (this.audioContext?.state !== 'closed') {
      this.audioContext?.close().catch(() => {});
    }
    this.audioContext = null;
    this.gainNode = null;
  }

  private ensureContext(sampleRate: number): void {
    if (this.audioContext) return;

    const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext;
    this.audioContext = new AudioContextClass({ sampleRate });
    this.gainNode = this.audioContext.createGain();
    this.gainNode.connect(this.audioContext.destination);
    this.scheduledTime = 0;
  }
}
