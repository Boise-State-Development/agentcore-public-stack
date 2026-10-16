import { Injectable, signal } from '@angular/core';
import { createAudioContext } from './audio-context';
import { float32ToPcm16, pcm16ToBase64, resampleLinear } from './pcm-utils';
import { VOICE_SAMPLE_RATE, SAMPLES_PER_CHUNK } from './voice.config';

/** A microphone the browser will let us capture from. */
export interface AudioInputDevice {
  deviceId: string;
  /** Empty until the user has granted microphone access once. */
  label: string;
}

/**
 * Per-device, not per-account: a device id is only meaningful in the browser
 * that enumerated it, so it lives in localStorage like the view-mode prefs.
 */
const INPUT_DEVICE_KEY = 'voice-input-device';

/**
 * Audio capture service using Web Audio API.
 *
 * Captures microphone input, resamples to 16kHz mono PCM, and emits
 * base64-encoded chunks at ~100ms intervals for Nova Sonic streaming.
 *
 * Uses AudioWorkletNode for off-main-thread audio processing.
 * The worklet processor is served from /audio/pcm-capture.worklet.js.
 */
@Injectable({ providedIn: 'root' })
export class AudioRecorderService {
  private readonly _isRecording = signal(false);
  private readonly _isSupported = signal(false);

  readonly isRecording = this._isRecording.asReadonly();
  readonly isSupported = this._isSupported.asReadonly();

  /**
   * The microphone to capture from, or null for the browser's default. Shared
   * by voice mode and dictation, which both record through this service.
   */
  private readonly _inputDeviceId = signal<string | null>(this.loadInputDeviceId());
  readonly inputDeviceId = this._inputDeviceId.asReadonly();

  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private sampleBuffer: Float32Array = new Float32Array(0);

  /** Callback invoked with each base64 PCM chunk */
  onAudioChunk: ((base64Pcm: string, sampleRate: number) => void) | null = null;

  /**
   * Callback invoked with each raw PCM16 chunk (same cadence as `onAudioChunk`).
   * Dictation sends binary WebSocket frames, so it skips the base64 step.
   */
  onPcmChunk: ((pcm: Int16Array) => void) | null = null;

  constructor() {
    this._isSupported.set(this.checkSupport());
  }

  private checkSupport(): boolean {
    if (typeof window === 'undefined') return false;
    const hasGetUserMedia = typeof navigator.mediaDevices?.getUserMedia === 'function';
    const hasAudioContext = !!(window.AudioContext || (window as any).webkitAudioContext);
    return hasGetUserMedia && hasAudioContext;
  }

  /** Choose the microphone for the next capture; null returns to the browser default. */
  setInputDevice(deviceId: string | null): void {
    this._inputDeviceId.set(deviceId);
    try {
      if (deviceId) {
        localStorage.setItem(INPUT_DEVICE_KEY, deviceId);
      } else {
        localStorage.removeItem(INPUT_DEVICE_KEY);
      }
    } catch {
      // Private mode or blocked storage: the choice still applies this visit.
    }
  }

  /**
   * The microphones this browser can see. Labels are blank until the user has
   * granted microphone access once, which is why the picker says so instead
   * of listing sixteen "Microphone" rows.
   */
  async listInputDevices(): Promise<AudioInputDevice[]> {
    if (typeof navigator.mediaDevices?.enumerateDevices !== 'function') return [];
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices
        .filter((d) => d.kind === 'audioinput' && d.deviceId)
        .map((d) => ({ deviceId: d.deviceId, label: d.label }));
    } catch {
      return [];
    }
  }

  /**
   * Start capturing audio from the microphone.
   * Requests microphone permission if not already granted.
   */
  async start(): Promise<void> {
    if (this._isRecording()) return;
    if (!this._isSupported()) {
      throw new Error('Audio recording not supported in this browser');
    }

    try {
      this.mediaStream = await this.openMicrophone();

      this.audioContext = createAudioContext(VOICE_SAMPLE_RATE);

      await this.audioContext.audioWorklet.addModule('/audio/pcm-capture.worklet.js');

      this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);

      this.workletNode = new AudioWorkletNode(this.audioContext, 'pcm-capture');
      this.sampleBuffer = new Float32Array(0);

      this.workletNode.port.onmessage = (event: MessageEvent<Float32Array>) => {
        this.processAudioChunk(event.data);
      };

      this.sourceNode.connect(this.workletNode);
      this.workletNode.connect(this.audioContext.destination);

      this._isRecording.set(true);
    } catch (err) {
      this.cleanup();
      throw err;
    }
  }

  /**
   * Open the chosen microphone, falling back to the default when it is gone.
   *
   * A stored device id outlives the device: unplug the headset and `exact`
   * throws OverconstrainedError, which would turn every later voice session
   * into an error toast until the user found the picker. The fallback clears
   * the stale choice so the picker shows the truth.
   *
   * `deviceId` must be the only mandatory constraint, or the fallback misreads
   * a constraint the device can't meet as a missing device and clears a good
   * choice. Mono is `ideal`, not `exact`: Chrome rejects `exact: 1` on an input
   * that only delivers stereo, where Firefox downmixes. A stereo track is fine,
   * because the capture worklet reads channel 0 only.
   */
  private async openMicrophone(): Promise<MediaStream> {
    const base: MediaTrackConstraints = {
      sampleRate: { ideal: VOICE_SAMPLE_RATE },
      channelCount: { ideal: 1 },
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    };
    const deviceId = this._inputDeviceId();
    if (deviceId) {
      try {
        return await navigator.mediaDevices.getUserMedia({
          audio: { ...base, deviceId: { exact: deviceId } },
        });
      } catch (err) {
        if (!(err instanceof DOMException) || err.name !== 'OverconstrainedError') throw err;
        this.setInputDevice(null);
      }
    }
    return navigator.mediaDevices.getUserMedia({ audio: base });
  }

  private loadInputDeviceId(): string | null {
    try {
      return localStorage.getItem(INPUT_DEVICE_KEY);
    } catch {
      return null;
    }
  }

  /** Stop capturing and release resources. */
  async stop(): Promise<void> {
    if (!this._isRecording()) return;
    this.flushBuffer();
    this.cleanup();
    this._isRecording.set(false);
  }

  private processAudioChunk(inputSamples: Float32Array): void {
    // Resample if browser's actual rate differs from target
    let samples = inputSamples;
    if (this.audioContext && this.audioContext.sampleRate !== VOICE_SAMPLE_RATE) {
      samples = resampleLinear(inputSamples, this.audioContext.sampleRate, VOICE_SAMPLE_RATE);
    }

    // Append to buffer
    const newBuffer = new Float32Array(this.sampleBuffer.length + samples.length);
    newBuffer.set(this.sampleBuffer);
    newBuffer.set(samples, this.sampleBuffer.length);
    this.sampleBuffer = newBuffer;

    // Emit complete chunks
    while (this.sampleBuffer.length >= SAMPLES_PER_CHUNK) {
      const chunk = this.sampleBuffer.slice(0, SAMPLES_PER_CHUNK);
      this.sampleBuffer = this.sampleBuffer.slice(SAMPLES_PER_CHUNK);

      this.emit(float32ToPcm16(chunk));
    }
  }

  /** Flush any remaining samples in the buffer as a final chunk. */
  private flushBuffer(): void {
    if (this.sampleBuffer.length > 0) {
      this.emit(float32ToPcm16(this.sampleBuffer));
      this.sampleBuffer = new Float32Array(0);
    }
  }

  private emit(pcm: Int16Array): void {
    this.onPcmChunk?.(pcm);
    if (this.onAudioChunk) {
      this.onAudioChunk(pcm16ToBase64(pcm), VOICE_SAMPLE_RATE);
    }
  }

  private cleanup(): void {
    this.workletNode?.disconnect();
    this.sourceNode?.disconnect();
    this.mediaStream?.getTracks().forEach(t => t.stop());
    if (this.audioContext?.state !== 'closed') {
      this.audioContext?.close().catch(() => {});
    }
    this.workletNode = null;
    this.sourceNode = null;
    this.mediaStream = null;
    this.audioContext = null;
    this.sampleBuffer = new Float32Array(0);
  }
}
