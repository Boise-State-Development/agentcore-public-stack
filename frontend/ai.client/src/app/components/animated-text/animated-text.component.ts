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
 */
export type AnimatedTextEntry = 'typewriter' | 'decode' | 'focus' | 'drop' | 'flip' | 'aurora';

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
  letters: Letter[];
}

const DECODE_GLYPHS = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&*+=?<>/\\';

/** How many characters the cipher noise runs ahead of the letters already locked. */
const DECODE_LEAD = 8;

/** The whole line should land in about this long, however long the text is. */
const STAGGER_BUDGET_MS = 1200;

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
        >
          @for (word of words(); track word.key; let last = $last) {
            <span class="word">
              @for (l of word.letters; track l.i) {
                @if (entry() === 'decode') {
                  <span class="ch" [class.locked]="l.i < decodeLocked()">
                    <span class="final">{{ l.c }}</span>
                    @if (l.i >= decodeLocked() && l.i < decodeSpawned()) {
                      <span class="glyph">{{ decodeGlyphs()[l.i] }}</span>
                    }
                  </span>
                } @else {
                  <span class="ch" [style.--i]="l.i" [style.--r]="l.r">{{ l.c }}</span>
                }
              }
            </span>
            @if (!last) { {{ ' ' }} }
          }
        </span>
      }
    </h1>
  `,
  styles: [`
    /* ---- typewriter ---------------------------------------------------- */
    @keyframes caret-blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0; }
    }
    .cursor {
      animation: caret-blink 1s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }

    /* ---- shared -------------------------------------------------------- */
    .word {
      display: inline-block;
      white-space: nowrap;
    }
    .ch {
      display: inline-block;
      animation-duration: 700ms;
      animation-fill-mode: both;
      animation-delay: calc(var(--i, 0) * var(--step, 40ms));
    }

    /* ---- decode: cipher noise locks into letters ------------------------ */
    [data-entry='decode'] .ch {
      position: relative;
      animation: none;
    }
    [data-entry='decode'] .final {
      visibility: hidden;
    }
    [data-entry='decode'] .locked .final {
      visibility: visible;
      animation: decode-lock 420ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    }
    [data-entry='decode'] .glyph {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.82em;
      font-weight: 500;
      color: var(--color-primary-500);
      opacity: 0.75;
    }
    :host-context(.dark) [data-entry='aurora'] {
      --aurora-settle: oklch(from var(--color-primary-500) 0.75 c h);
    }
    :host-context(.dark) [data-entry='decode'] {
      --decode-flash: oklch(from var(--color-primary-500) 0.8 c h);
    }
    :host-context(.dark) [data-entry='decode'] .glyph {
      color: oklch(from var(--color-primary-500) 0.8 c h);
      opacity: 0.85;
    }
    @keyframes decode-lock {
      0% {
        color: var(--decode-flash, var(--color-primary-500));
        transform: scale(1.25);
        text-shadow: 0 0 18px oklch(from var(--color-primary-500) l c h / 0.7);
      }
    }

    /* ---- focus: rack focus from a heavy blur ---------------------------- */
    [data-entry='focus'] .ch {
      animation-name: focus-in;
      animation-duration: 1100ms;
      animation-timing-function: cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes focus-in {
      0% {
        opacity: 0;
        filter: blur(16px);
        transform: translateY(0.35em) scale(1.6);
      }
      45% {
        opacity: 1;
      }
      100% {
        opacity: 1;
        filter: blur(0);
        transform: none;
      }
    }

    /* ---- drop: gravity, squash and bounce ------------------------------- */
    [data-entry='drop'] .ch {
      transform-origin: 50% 100%;
      animation-name: drop-in;
      animation-duration: 900ms;
      animation-timing-function: linear;
    }
    @keyframes drop-in {
      0% {
        opacity: 0;
        transform: translateY(-1.6em) rotate(calc(var(--r, 0) * 35deg));
        animation-timing-function: cubic-bezier(0.55, 0, 1, 0.45);
      }
      10% { opacity: 1; }
      42% {
        transform: translateY(0) rotate(0) scale(1.18, 0.78);
        animation-timing-function: cubic-bezier(0.33, 1, 0.68, 1);
      }
      62% {
        transform: translateY(-0.22em) rotate(calc(var(--r, 0) * -6deg)) scale(0.96, 1.05);
        animation-timing-function: cubic-bezier(0.32, 0, 0.67, 0);
      }
      78% {
        transform: translateY(0) scale(1.05, 0.94);
        animation-timing-function: cubic-bezier(0.33, 1, 0.68, 1);
      }
      90% { transform: translateY(-0.05em) scale(1); }
      100% { opacity: 1; transform: none; }
    }

    /* ---- flip: split-flap departure board ------------------------------- */
    [data-entry='flip'] .word {
      perspective: 500px;
    }
    [data-entry='flip'] .ch {
      transform-origin: 50% 0%;
      backface-visibility: hidden;
      border-radius: 0.12em;
      padding-inline: 0.02em;
      margin-inline: -0.02em;
      animation-name: flip-in, flip-tile;
      animation-duration: 650ms, 1100ms;
      animation-timing-function: cubic-bezier(0.34, 1.56, 0.64, 1), ease-out;
    }
    @keyframes flip-in {
      0% {
        opacity: 0;
        transform: rotateX(-105deg);
      }
      30% { opacity: 1; }
      100% {
        opacity: 1;
        transform: rotateX(0);
      }
    }
    @keyframes flip-tile {
      0%, 45% {
        background-color: color-mix(in oklch, currentColor 12%, transparent);
        box-shadow: inset 0 -0.5px 0 0 color-mix(in oklch, currentColor 25%, transparent);
      }
      100% {
        background-color: transparent;
        box-shadow: inset 0 0 0 0 transparent;
      }
    }

    /* ---- aurora: ignite in a hue-shifted glow, cool to the text colour -- */
    [data-entry='aurora'] .ch {
      animation-name: aurora-in;
      animation-duration: 1400ms;
      animation-timing-function: cubic-bezier(0.22, 1, 0.36, 1);
    }
    @keyframes aurora-in {
      0% {
        opacity: 0;
        filter: blur(6px);
        transform: translateX(-0.4em) skewX(-18deg);
        color: oklch(from var(--color-primary-500) 0.8 c calc(h + 140));
        text-shadow: 0 0 0 transparent;
      }
      25% {
        opacity: 1;
        filter: blur(0);
        transform: none;
        color: oklch(from var(--color-primary-500) 0.72 0.2 calc(h + 70));
        text-shadow: 0 0 22px oklch(from var(--color-primary-500) 0.7 0.2 calc(h + 70) / 0.9);
      }
      55% {
        color: var(--aurora-settle, var(--color-primary-500));
        text-shadow: 0 0 14px oklch(from var(--color-primary-500) l c h / 0.6);
      }
      100% {
        opacity: 1;
        text-shadow: 0 0 0 transparent;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .ch,
      .cursor,
      [data-entry='decode'] .locked .final {
        animation: none !important;
      }
    }
  `],
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
        letters: Array.from(w, c => {
          const letter = { c, i, r: wobble(i) };
          i++;
          return letter;
        }),
      }));
  });

  private readonly letterCount = computed(() =>
    this.words().reduce((n, w) => n + w.letters.length, 0),
  );

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
