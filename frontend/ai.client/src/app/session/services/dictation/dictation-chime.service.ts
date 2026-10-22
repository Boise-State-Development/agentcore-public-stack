import { Injectable } from '@angular/core';

/** Which edge of a dictation the chime marks. */
export type DictationChime = 'start' | 'stop';

/** E5 and B5: a fifth apart, so the pair reads as one gesture played both ways. */
const LOW_HZ = 659.25;
const HIGH_HZ = 987.77;

/** Onset of the second note after the first. */
const SECOND_NOTE_DELAY_S = 0.085;

/** Peak gain of each fundamental — quiet enough to sit under speech, not over it. */
const PEAK_GAIN = 0.12;

/** A faint octave above each note, for a little brightness on laptop speakers. */
const OVERTONE_GAIN = 0.02;

type AudioContextCtor = typeof AudioContext;

/**
 * The two short tones that bracket a dictation: rising when the mic is live,
 * falling when it is off.
 *
 * Synthesized with Web Audio rather than shipped as files — two sine notes
 * need no asset, no fetch, and no decode latency on the first press.
 *
 * Browsers only let a page make sound after a user gesture, and the start
 * chime plays once the transcription socket is up, well after the click. So
 * `prime()` creates (or resumes) the context inside the click, and `play()`
 * reuses it later. Everything here is best-effort: no Web Audio (jsdom, an
 * old browser) or a context the browser refuses simply means no chime, never
 * a failed dictation.
 */
@Injectable({ providedIn: 'root' })
export class DictationChimeService {
  private context: AudioContext | null = null;

  /** Create or resume the audio context. Call from inside the user's click. */
  prime(): void {
    this.audio();
  }

  play(chime: DictationChime): void {
    const context = this.audio();
    if (!context) return;
    const [first, second] = chime === 'start' ? [LOW_HZ, HIGH_HZ] : [HIGH_HZ, LOW_HZ];
    const t = context.currentTime + 0.01;
    this.tone(context, first, t, 0.1, PEAK_GAIN);
    this.tone(context, first * 2, t, 0.06, OVERTONE_GAIN);
    this.tone(context, second, t + SECOND_NOTE_DELAY_S, 0.15, PEAK_GAIN);
    this.tone(context, second * 2, t + SECOND_NOTE_DELAY_S, 0.08, OVERTONE_GAIN);
  }

  private audio(): AudioContext | null {
    try {
      if (!this.context) {
        const Ctor = audioContextCtor();
        if (!Ctor) return null;
        this.context = new Ctor();
      }
      if (this.context.state === 'suspended') {
        void this.context.resume().catch(() => undefined);
      }
      return this.context;
    } catch {
      return null;
    }
  }

  /** One enveloped sine: a 10ms attack so it does not click, then an exponential fall. */
  private tone(
    context: AudioContext,
    frequency: number,
    at: number,
    duration: number,
    peak: number,
  ): void {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(peak, at + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + duration);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(at);
    oscillator.stop(at + duration + 0.02);
  }
}

function audioContextCtor(): AudioContextCtor | null {
  const g = globalThis as typeof globalThis & {
    AudioContext?: AudioContextCtor;
    webkitAudioContext?: AudioContextCtor;
  };
  return g.AudioContext ?? g.webkitAudioContext ?? null;
}
