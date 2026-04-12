import { Injectable, signal, computed } from '@angular/core';
import { float32ToPcm16, pcm16ToBase64, resampleLinear, SAMPLES_PER_CHUNK } from './pcm-utils';

const TARGET_SAMPLE_RATE = 16000;

/**
 * Audio capture service using Web Audio API.
 *
 * Captures microphone input, resamples to 16kHz mono PCM, and emits
 * base64-encoded chunks at ~100ms intervals for Nova Sonic streaming.
 *
 * Uses ScriptProcessorNode (widely supported) with a buffer of 4096 samples.
 * AudioWorklet would be preferred for production but requires serving a
 * separate JS file — ScriptProcessorNode works without extra build config.
 */
@Injectable({ providedIn: 'root' })
export class AudioRecorderService {
  private readonly _isRecording = signal(false);
  private readonly _isSupported = signal(false);

  readonly isRecording = this._isRecording.asReadonly();
  readonly isSupported = this._isSupported.asReadonly();

  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private processorNode: ScriptProcessorNode | null = null;
  private sampleBuffer: Float32Array = new Float32Array(0);

  /** Callback invoked with each base64 PCM chunk */
  onAudioChunk: ((base64Pcm: string, sampleRate: number) => void) | null = null;

  constructor() {
    this._isSupported.set(this.checkSupport());
  }

  private checkSupport(): boolean {
    if (typeof window === 'undefined') return false;
    return !!(
      navigator.mediaDevices?.getUserMedia &&
      (window.AudioContext || (window as any).webkitAudioContext)
    );
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
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: { ideal: TARGET_SAMPLE_RATE },
          channelCount: { exact: 1 },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext;
      this.audioContext = new AudioContextClass({ sampleRate: TARGET_SAMPLE_RATE });

      this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);

      // ScriptProcessorNode: 4096 buffer, 1 input channel, 1 output channel
      this.processorNode = this.audioContext.createScriptProcessor(4096, 1, 1);
      this.sampleBuffer = new Float32Array(0);

      this.processorNode.onaudioprocess = (event: AudioProcessingEvent) => {
        const inputData = event.inputBuffer.getChannelData(0);
        this.processAudioChunk(inputData);
      };

      this.sourceNode.connect(this.processorNode);
      this.processorNode.connect(this.audioContext.destination);

      this._isRecording.set(true);
    } catch (err) {
      this.cleanup();
      throw err;
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
    if (this.audioContext && this.audioContext.sampleRate !== TARGET_SAMPLE_RATE) {
      samples = resampleLinear(inputSamples, this.audioContext.sampleRate, TARGET_SAMPLE_RATE);
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

      const pcm = float32ToPcm16(chunk);
      const base64 = pcm16ToBase64(pcm);
      this.onAudioChunk?.(base64, TARGET_SAMPLE_RATE);
    }
  }

  /** Flush any remaining samples in the buffer as a final chunk. */
  private flushBuffer(): void {
    if (this.sampleBuffer.length > 0) {
      const pcm = float32ToPcm16(this.sampleBuffer);
      const base64 = pcm16ToBase64(pcm);
      this.onAudioChunk?.(base64, TARGET_SAMPLE_RATE);
      this.sampleBuffer = new Float32Array(0);
    }
  }

  private cleanup(): void {
    this.processorNode?.disconnect();
    this.sourceNode?.disconnect();
    this.mediaStream?.getTracks().forEach(t => t.stop());
    if (this.audioContext?.state !== 'closed') {
      this.audioContext?.close().catch(() => {});
    }
    this.processorNode = null;
    this.sourceNode = null;
    this.mediaStream = null;
    this.audioContext = null;
    this.sampleBuffer = new Float32Array(0);
  }
}
