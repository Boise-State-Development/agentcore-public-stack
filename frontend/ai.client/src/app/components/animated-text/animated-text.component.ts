import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';

/**
 * How the text arrives.
 *
 * - `typewriter`: one character at a time behind a blinking caret (the original).
 * - `decode`: a field of cipher glyphs runs ahead and locks into the real letters, left to right.
 * - `focus`: each letter racks in from a heavy blur, oversized and low, then snaps sharp.
 * - `drop`: letters fall onto the baseline at a tilt, squash on impact and bounce settle.
 * - `flip`: a departure board; every letter flips down on its own tile, which then fades away.
 * - `aurora`: letters ignite in a hue-shifted glow of the brand colour and cool to the text colour.
 *
 * Bold:
 * - `lava`: brand-coloured blobs rise from under the line, fuse with their neighbours and tighten into letters.
 * - `blueprint`: each letter is drafted as an outline, floods with ink from the baseline up, then dries to the text colour.
 * - `warp`: letters streak in from the right as stretched light and snap into shape.
 * - `prism`: three colour channels slide together, and the letter appears where they meet.
 * - `shockwave`: a pulse bursts from the middle of the line and throws the letters outward before they settle.
 *
 * Quiet:
 * - `rise`: letters climb out from behind the baseline, one after another.
 * - `settle`: the whole line fades up with wide spacing that drifts closed.
 * - `weight`: hairline letters thicken to the heading weight (needs a variable font; a plain fade without one).
 * - `sheen`: the line fades up, then one band of brand-coloured light crosses it.
 * - `whisper`: word by word, each one easing out of a soft blur.
 */
export type AnimatedTextEntry =
  | 'typewriter'
  | 'decode'
  | 'focus'
  | 'drop'
  | 'flip'
  | 'aurora'
  | 'lava'
  | 'blueprint'
  | 'warp'
  | 'prism'
  | 'shockwave'
  | 'rise'
  | 'settle'
  | 'weight'
  | 'sheen'
  | 'whisper';

interface Letter {
  /** The character itself. */
  c: string;
  /** Index among the non-space characters, which drives the stagger. */
  i: number;
  /** A stable value in [-1, 1] per index, so a re-render never re-rolls the tilt. */
  r: number;
}

interface Word {
  key: string;
  /** Index among the words, which drives the word-by-word stagger. */
  i: number;
  letters: Letter[];
}

const DECODE_GLYPHS = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&*+=?<>/\\';

/** How many characters the cipher noise runs ahead of the letters already locked. */
const DECODE_LEAD = 8;

/** The whole line should land in about this long, however long the text is. */
const STAGGER_BUDGET_MS = 1200;

/** The same budget for the entries that stagger by word, which hold each word longer. */
const WORD_STAGGER_BUDGET_MS = 900;

/** The longest and shortest gap between words. */
const WORD_STEP_MAX_MS = 110;
const WORD_STEP_MIN_MS = 30;

/** Deterministic pseudo-random in [-1, 1] for an index. */
function wobble(i: number): number {
  const x = Math.sin((i + 1) * 12.9898) * 43758.5453;
  return (x - Math.floor(x)) * 2 - 1;
}

function randomGlyph(): string {
  return DECODE_GLYPHS[Math.floor(Math.random() * DECODE_GLYPHS.length)];
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

@Component({
  selector: 'app-animated-text',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h1 class="text-4xl/tight font-semibold dark:text-gray-100 text-gray-900">
      <span class="sr-only">{{ text() }}</span>
      @if (entry() === 'typewriter') {
        <span aria-hidden="true">{{ displayedText() }}@if (isAnimating() && showCursor()) {<span class="cursor">|</span>}</span>
      } @else {
        <span
          class="stage"
          aria-hidden="true"
          [attr.data-entry]="entry()"
          [style.--step]="stepMs() + 'ms'"
          [style.--word-step]="wordStepMs() + 'ms'"
          [style.--n]="letterCount()"
        >
          @if (entry() === 'lava') {
            <!-- Turns blurred letters into hard-edged blobs: alpha is multiplied up and cut off. -->
            <svg class="goo-defs" focusable="false">
              <filter
                id="animated-text-goo"
                x="-20%"
                y="-80%"
                width="140%"
                height="260%"
                color-interpolation-filters="sRGB"
              >
                <feColorMatrix type="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 22 -8" />
              </filter>
            </svg>
          }
          @for (word of words(); track word.key; let last = $last) {
            <span class="word" [style.--w]="word.i">
              @for (l of word.letters; track l.i) {
                @if (entry() === 'decode') {
                  <span class="ch" [class.locked]="l.i < decodeLocked()">
                    <span class="final">{{ l.c }}</span>
                    @if (l.i >= decodeLocked() && l.i < decodeSpawned()) {
                      <span class="glyph">{{ decodeGlyphs()[l.i] }}</span>
                    }
                  </span>
                } @else {
                  <span class="ch" [attr.data-c]="l.c" [style.--i]="l.i" [style.--r]="l.r">{{ l.c }}</span>
                }
              }
            </span>
            @if (!last) { {{ ' ' }} }
          }
        </span>
      }
    </h1>
  `,
  styleUrl: './animated-text.component.css',
})
export class AnimatedTextComponent {
  /** The text to animate. */
  text = input.required<string>();

  /** Which entry animation to play. */
  entry = input<AnimatedTextEntry>('typewriter');

  /**
   * Milliseconds per character. For the per-letter effects this is the stagger, capped
   * so a long line still lands in about a second.
   */
  speed = input<number>(50);

  /** Whether the typewriter shows its caret while typing. */
  showCursor = input<boolean>(true);

  /** Typewriter: the text typed so far. */
  displayedText = signal('');

  /** Typewriter: whether it is still typing. */
  isAnimating = signal(true);

  /** Decode: letters at an index below this have locked in. */
  protected readonly decodeLocked = signal(0);

  /** Decode: letters at an index below this are showing noise (or are locked). */
  protected readonly decodeSpawned = signal(0);

  /** Decode: the noise glyph currently shown at each index. */
  protected readonly decodeGlyphs = signal<string[]>([]);

  private run = 0;
  private timerId?: ReturnType<typeof setTimeout>;

  /**
   * The text split into words of letters. The keys carry a run number, so new text
   * re-creates the elements and their CSS animations play again.
   */
  protected readonly words = computed<Word[]>(() => {
    const run = ++this.run;
    let i = 0;
    return this.text()
      .split(/\s+/)
      .filter(w => w.length > 0)
      .map((w, wi) => ({
        key: `${run}-${wi}`,
        i: wi,
        letters: Array.from(w, c => {
          const letter = { c, i, r: wobble(i) };
          i++;
          return letter;
        }),
      }));
  });

  protected readonly letterCount = computed(() =>
    this.words().reduce((n, w) => n + w.letters.length, 0),
  );

  /** The gap between words for the entries that stagger by word, capped like the letter stagger. */
  protected readonly wordStepMs = computed(() => {
    const n = Math.max(1, this.words().length);
    return Math.round(
      Math.max(WORD_STEP_MIN_MS, Math.min(WORD_STEP_MAX_MS, WORD_STAGGER_BUDGET_MS / n)),
    );
  });

  protected readonly stepMs = computed(() => {
    const n = Math.max(1, this.letterCount());
    return Math.round(Math.max(12, Math.min(this.speed(), STAGGER_BUDGET_MS / n)));
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => this.stopTimer());

    effect(() => {
      const fullText = this.text();
      const entry = this.entry();
      const speed = this.speed();
      const total = this.letterCount();

      this.stopTimer();
      const reduced = prefersReducedMotion();
      const step = this.stepMs();

      // Starting a run reads and writes its own state; none of that may become a
      // dependency, or every tick would restart the run.
      untracked(() => this.start(fullText, entry, speed, step, total, reduced));
    });
  }

  private start(
    fullText: string,
    entry: AnimatedTextEntry,
    speed: number,
    step: number,
    total: number,
    reduced: boolean,
  ): void {
    if (entry === 'typewriter') {
      if (reduced) {
        this.displayedText.set(fullText);
        this.isAnimating.set(false);
        return;
      }
      this.displayedText.set('');
      this.isAnimating.set(true);
      this.typeFrom(fullText, 0, speed);
      return;
    }

    if (entry === 'decode') {
      this.decodeGlyphs.set(Array.from({ length: total }, randomGlyph));
      if (reduced) {
        this.decodeLocked.set(total);
        this.decodeSpawned.set(total);
        return;
      }
      this.decodeLocked.set(0);
      this.decodeSpawned.set(0);
      this.decodeTick(total, step);
    }
  }

  private stopTimer(): void {
    if (this.timerId !== undefined) {
      clearTimeout(this.timerId);
      this.timerId = undefined;
    }
  }

  private typeFrom(fullText: string, index: number, speed: number): void {
    if (index >= fullText.length) {
      this.isAnimating.set(false);
      return;
    }
    this.displayedText.update(t => t + fullText[index]);
    this.timerId = setTimeout(() => this.typeFrom(fullText, index + 1, speed), speed);
  }

  /**
   * One frame of the decode: the noise front runs ahead two letters a frame, the lock
   * front follows one a frame once the noise has a lead, and every unlocked glyph re-rolls.
   */
  private decodeTick(total: number, step: number): void {
    const spawned = Math.min(total, this.decodeSpawned() + 2);
    this.decodeSpawned.set(spawned);
    if (spawned >= Math.min(total, DECODE_LEAD)) {
      this.decodeLocked.update(n => Math.min(total, n + 1));
    }
    const locked = this.decodeLocked();
    this.decodeGlyphs.update(glyphs =>
      glyphs.map((g, i) => (i >= locked && i < spawned ? randomGlyph() : g)),
    );
    if (locked >= total) {
      return;
    }
    this.timerId = setTimeout(() => this.decodeTick(total, step), step);
  }
}
