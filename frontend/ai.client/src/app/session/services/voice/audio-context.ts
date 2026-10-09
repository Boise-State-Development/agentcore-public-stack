/**
 * Open an AudioContext at `sampleRate`, or at the device's own rate when the
 * browser refuses it.
 *
 * Some Chrome and hardware combinations throw NotSupportedError for a rate the
 * device can't run at, while Firefox resamples quietly. Asking for the voice
 * rate first keeps the common path free of our own resampling; on refusal,
 * the native-rate context still works because the recorder resamples its
 * chunks and the player's buffers are resampled by Web Audio itself.
 */
export function createAudioContext(sampleRate: number): AudioContext {
  const AudioContextClass: typeof AudioContext =
    window.AudioContext || (window as any).webkitAudioContext;
  try {
    return new AudioContextClass({ sampleRate });
  } catch (err) {
    if (!(err instanceof DOMException) || err.name !== 'NotSupportedError') throw err;
    return new AudioContextClass();
  }
}

/**
 * A toast-ready description of a microphone or audio failure.
 *
 * Browser media errors carry their cause in `name` (OverconstrainedError,
 * NotAllowedError, NotReadableError) and often a blank or generic `message`,
 * so the name leads. A field report then says which failure it was.
 */
export function describeAudioError(err: unknown, fallback: string): string {
  if (err instanceof DOMException) {
    return err.message ? `${err.name}: ${err.message}` : err.name;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}
