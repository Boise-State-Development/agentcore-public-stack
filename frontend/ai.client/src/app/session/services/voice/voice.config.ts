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

// --- Echo gate (#2019) ---
//
// While the agent's audio is playing, the microphone also hears the speakers,
// and the browser's echo canceller does not always remove it (Firefox on
// Windows with a USB mic let it through with echoCancellation applied). Nova
// finishes generating before playback ends, so that echo reaches Nova as a new
// user turn. The gate sends silence instead of mic audio while the agent is
// audible, and opens only for speech louder than the interrupt level.

/**
 * Default interrupt level in dBFS (0 is full scale). Speech close to a desk
 * mic sits around -20 to -10 dBFS; speaker echo that survives the browser's
 * canceller is usually well below that. The user can move it with the slider
 * in the voice overlay, and the choice is kept per browser.
 */
export const INTERRUPT_LEVEL_DEFAULT_DB = -26;

/** Slider bounds for the interrupt level, in dBFS. */
export const INTERRUPT_LEVEL_MIN_DB = -50;
export const INTERRUPT_LEVEL_MAX_DB = -5;

/** Lowest level the mic meter shows; anything quieter reads as empty. */
export const MIC_METER_FLOOR_DB = -60;

/**
 * Consecutive chunks above the interrupt level before the gate opens.
 * 2 chunks (200 ms) rejects clicks and short echo peaks without making an
 * interruption feel slow.
 */
export const INTERRUPT_OPEN_CHUNKS = 2;

/**
 * Chunks kept while gated and sent when the gate opens, so the start of the
 * interruption reaches Nova instead of being replaced by silence. Must be at
 * least INTERRUPT_OPEN_CHUNKS so the chunks that opened the gate are included.
 */
export const INTERRUPT_PRE_ROLL_CHUNKS = 3;

/**
 * How long the gate stays closed after playback ends. Covers the speaker and
 * room tail and a short gap between audio chunks arriving over the network.
 */
export const ECHO_TAIL_MS = 400;

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
