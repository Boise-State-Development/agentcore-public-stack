/**
 * PCM audio encoding/decoding utilities for Nova Sonic voice streaming.
 *
 * All functions are pure — no state, no services, no side effects.
 * Transport format: 16-bit PCM encoded as base64 over JSON WebSocket.
 */

export { SAMPLES_PER_CHUNK, VOICE_SAMPLE_RATE } from './voice.config';

/**
 * Convert Float32 audio samples (-1.0 to 1.0) to 16-bit PCM Int16Array.
 * Clamps values to prevent distortion.
 */
export function float32ToPcm16(samples: Float32Array): Int16Array {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF;
  }
  return pcm;
}

/**
 * Convert 16-bit PCM Int16Array to Float32 audio samples (-1.0 to 1.0).
 */
export function pcm16ToFloat32(pcm: Int16Array): Float32Array {
  const float = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) {
    float[i] = pcm[i] / (pcm[i] < 0 ? 0x8000 : 0x7FFF);
  }
  return float;
}

/**
 * Encode 16-bit PCM as base64 string for WebSocket transmission.
 */
export function pcm16ToBase64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Decode base64 string to 16-bit PCM Int16Array.
 */
export function base64ToPcm16(b64: string): Int16Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Int16Array(bytes.buffer);
}

/**
 * Linear interpolation resampling.
 * Used when browser's native sample rate differs from target (16kHz).
 */
export function resampleLinear(
  samples: Float32Array,
  fromRate: number,
  toRate: number
): Float32Array {
  if (fromRate === toRate) return samples;

  const ratio = fromRate / toRate;
  const outputLength = Math.round(samples.length / ratio);
  const output = new Float32Array(outputLength);

  for (let i = 0; i < outputLength; i++) {
    const srcIndex = i * ratio;
    const srcFloor = Math.floor(srcIndex);
    const srcCeil = Math.min(srcFloor + 1, samples.length - 1);
    const frac = srcIndex - srcFloor;
    output[i] = samples[srcFloor] * (1 - frac) + samples[srcCeil] * frac;
  }

  return output;
}

/**
 * Convenience: Float32 samples → base64 PCM string (for sending to server).
 */
export function samplesToBase64(samples: Float32Array): string {
  return pcm16ToBase64(float32ToPcm16(samples));
}

/**
 * Convenience: base64 PCM string → Float32 samples (for playback).
 */
export function base64ToSamples(b64: string): Float32Array {
  return pcm16ToFloat32(base64ToPcm16(b64));
}
