/**
 * Voice mode configuration constants.
 *
 * Centralizes all tunable values for the voice pipeline so they
 * can be found and adjusted in one place.
 */

/** Audio sample rate in Hz. Nova Sonic expects 16kHz mono PCM. */
export const VOICE_SAMPLE_RATE = 16000;

/** Duration of each audio chunk in milliseconds. */
export const CHUNK_DURATION_MS = 100;

/** Number of samples per chunk (sampleRate * chunkDuration / 1000). */
export const SAMPLES_PER_CHUNK = (VOICE_SAMPLE_RATE * CHUNK_DURATION_MS) / 1000; // 1600

/** Auto-disconnect after this many milliseconds of silence. */
export const IDLE_TIMEOUT_MS = 60_000;

/** WebSocket connection timeout in milliseconds. */
export const WS_CONNECT_TIMEOUT_MS = 10_000;

/** Maximum transcript entries kept in memory per session. */
export const MAX_TRANSCRIPT_ENTRIES = 200;

/**
 * Transcript reveal speed — characters per tick while the agent is speaking.
 * ~20 chars/sec at 50ms ticks ≈ natural speaking pace.
 */
export const REVEAL_CHARS_PER_TICK = 2;

/** Interval in ms between reveal ticks. */
export const REVEAL_TICK_MS = 50;

/** Characters per tick when flushing remaining text after response completes. */
export const REVEAL_FLUSH_CHARS_PER_TICK = 5;

/** WebSocket close codes and their user-facing meanings. */
export const WS_CLOSE_REASONS: Record<number, string> = {
  1000: 'Session ended normally',
  1001: 'Server shutting down',
  1006: 'Connection lost — check your network',
  1008: 'Policy violation',
  1011: 'Server error',
  4001: 'Authentication failed',
  4003: 'Forbidden',
  4008: 'Session timeout',
  4029: 'Rate limited — try again shortly',
};
