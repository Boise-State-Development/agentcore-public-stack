import {
  Component,
  ChangeDetectionStrategy,
  signal,
  computed,
  input,
  OnInit,
  OnDestroy,
} from '@angular/core';

/**
 * University-themed loading phrases for the typewriter effect
 */
const LOADING_PHRASES = [
  'Forming a hypothesis',
  'Calculating',
  'Inferring',
  'Deriving',
  'Researching',
  'Analyzing data',
  'Reviewing literature',
  'Running experiments',
  'Consulting the archives',
  'Checking citations',
  'Cross-referencing',
  'Synthesizing findings',
  'Examining variables',
  'Testing assumptions',
  'Evaluating evidence',
  'Compiling results',
  'Pondering',
  'Deliberating',
  'Theorizing',
  'Extrapolating',
];

/**
 * PulsatingLoaderComponent
 *
 * A loading indicator featuring a pulsing circle with expanding ring effect
 * and a typewriter-style text animation. The text cycles through university-themed
 * loading phrases, typing in character by character, pausing, then deleting
 * before showing the next phrase.
 *
 * When `notice` is set, the playful cycling stops and the loader states a
 * specific fact instead — used when the backend is retrying a failed model
 * call. A retry is otherwise indistinguishable from a hang, and cheerful
 * phrases like "Pondering..." during a provider outage actively mislead.
 *
 * @example
 * ```html
 * <app-pulsating-loader />
 * <app-pulsating-loader [notice]="'The model is busy. Retrying…'" />
 * ```
 */
@Component({
  selector: 'app-pulsating-loader',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="flex items-center gap-4"
      role="status"
      [attr.aria-busy]="true"
      [attr.aria-live]="notice() ? 'polite' : null"
      [attr.aria-label]="'Loading: ' + displayText()"
    >
      <!-- Pulsing circle with ring effect -->
      <div class="pulsing-circle" [class.is-notice]="!!notice()" aria-hidden="true"></div>

      <!-- Typewriter text with cursor. A notice replaces the cycling phrases
           outright — and drops the cursor, which would read as "still typing"
           on text that is finished. -->
      <div class="flex items-center">
        <span
          class="text-base/6 font-medium"
          [class]="notice()
            ? 'text-amber-700 dark:text-amber-400'
            : 'text-secondary-600 dark:text-secondary-400'"
        >
          {{ displayText() }}
        </span>
        @if (!notice()) {
          <span
            class="typing-cursor ml-0.5 text-secondary-600 dark:text-secondary-400"
            aria-hidden="true"
          >|</span>
        }
      </div>
    </div>

    <style>
      :host {
        display: block;
      }

      .pulsing-circle {
        position: relative;
        width: 12px;
        height: 12px;
        flex-shrink: 0;
      }

      .pulsing-circle::before {
        content: '';
        position: relative;
        display: block;
        width: 300%;
        height: 300%;
        box-sizing: border-box;
        margin-left: -100%;
        margin-top: -100%;
        border-radius: 50%;
        background-color: var(--color-secondary-500);
        animation: pulse-ring 1.25s cubic-bezier(0.215, 0.61, 0.355, 1) infinite;
      }

      .pulsing-circle::after {
        content: '';
        position: absolute;
        left: 0;
        top: 0;
        display: block;
        width: 100%;
        height: 100%;
        background-color: var(--color-secondary-500);
        border-radius: 50%;
        box-shadow: 0 0 8px var(--color-secondary-500 / 0.4);
        animation: pulse-dot 1.25s cubic-bezier(0.455, 0.03, 0.515, 0.955) -0.4s infinite;
      }

      /* Notice state: same pulse, different signal colour. The dot is the
         only thing a user tracks peripherally, so it has to change too —
         swapping just the text leaves the indicator looking routine. */
      .pulsing-circle.is-notice::before,
      .pulsing-circle.is-notice::after {
        background-color: var(--color-amber-500);
      }

      .pulsing-circle.is-notice::after {
        box-shadow: 0 0 8px var(--color-amber-500);
      }

      @keyframes pulse-ring {
        0% {
          transform: scale(0.33);
        }
        80%, 100% {
          opacity: 0;
        }
      }

      @keyframes pulse-dot {
        0% {
          transform: scale(0.8);
        }
        50% {
          transform: scale(1);
        }
        100% {
          transform: scale(0.8);
        }
      }

      /* Dark mode - use lighter secondary shade */
      :host-context(.dark) .pulsing-circle::before {
        background-color: var(--color-secondary-400);
      }

      :host-context(.dark) .pulsing-circle::after {
        background-color: var(--color-secondary-400);
        box-shadow: 0 0 8px var(--color-secondary-400 / 0.5);
      }

      .typing-cursor {
        animation: cursor-blink 0.7s step-end infinite;
        font-weight: 400;
      }

      @keyframes cursor-blink {
        0%, 100% {
          opacity: 1;
        }
        50% {
          opacity: 0;
        }
      }
    </style>
  `,
})
export class PulsatingLoaderComponent implements OnInit, OnDestroy {
  /**
   * Fixed message that replaces the cycling phrases, e.g. a retry in
   * progress. Null (the default) keeps the normal typewriter behaviour.
   */
  notice = input<string | null>(null);

  // Base timing constants (in milliseconds)
  private readonly TYPE_SPEED_BASE = 45;
  private readonly TYPE_SPEED_VARIANCE = 35;
  private readonly DELETE_SPEED_BASE = 15;
  private readonly DELETE_SPEED_VARIANCE = 10;
  private readonly PAUSE_AFTER_TYPING = 1200;
  private readonly PAUSE_AFTER_DELETING = 300;

  // Characters that cause slight hesitation (less common, harder to reach)
  private readonly SLOW_CHARS = new Set(['z', 'x', 'q', 'j', 'k', 'v', 'b', 'p', 'y', 'w']);
  // Characters that flow quickly (home row, common)
  private readonly FAST_CHARS = new Set(['a', 's', 'd', 'f', 'e', 'r', 't', 'i', 'o', 'n', ' ']);

  // State signals
  private currentPhraseIndex = signal(0);
  private currentCharIndex = signal(0);
  private isDeleting = signal(false);
  private isPaused = signal(false);

  // Timer reference for cleanup
  private animationTimer: ReturnType<typeof setTimeout> | null = null;

  // Computed display text with ellipsis. A notice wins outright; the
  // typewriter keeps ticking underneath so it resumes cleanly when the
  // notice clears mid-turn.
  displayText = computed(() => {
    const notice = this.notice();
    if (notice) {
      return notice;
    }
    const phrase = LOADING_PHRASES[this.currentPhraseIndex()] + '...';
    return phrase.substring(0, this.currentCharIndex());
  });

  ngOnInit(): void {
    this.startAnimation();
  }

  ngOnDestroy(): void {
    if (this.animationTimer) {
      clearTimeout(this.animationTimer);
    }
  }

  private startAnimation(): void {
    this.tick();
  }

  private tick(): void {
    const currentPhrase = LOADING_PHRASES[this.currentPhraseIndex()] + '...';
    const charIndex = this.currentCharIndex();
    const deleting = this.isDeleting();

    if (this.isPaused()) {
      return; // Wait for pause to complete
    }

    if (!deleting) {
      // Typing mode
      if (charIndex < currentPhrase.length) {
        // Type next character
        this.currentCharIndex.update((v) => v + 1);
        const nextChar = currentPhrase[charIndex] || '';
        this.scheduleNextTick(this.getTypingDelay(nextChar));
      } else {
        // Finished typing, pause then start deleting
        this.isPaused.set(true);
        this.animationTimer = setTimeout(() => {
          this.isPaused.set(false);
          this.isDeleting.set(true);
          this.tick();
        }, this.PAUSE_AFTER_TYPING);
      }
    } else {
      // Deleting mode (faster, less variance - like holding backspace)
      if (charIndex > 0) {
        // Delete previous character
        this.currentCharIndex.update((v) => v - 1);
        this.scheduleNextTick(this.getDeletingDelay());
      } else {
        // Finished deleting, pause then move to next phrase
        this.isPaused.set(true);
        this.animationTimer = setTimeout(() => {
          this.isPaused.set(false);
          this.isDeleting.set(false);
          // Move to next phrase (random selection)
          this.selectNextPhrase();
          this.tick();
        }, this.PAUSE_AFTER_DELETING);
      }
    }
  }

  /**
   * Calculate typing delay based on character difficulty and randomness
   */
  private getTypingDelay(char: string): number {
    const lowerChar = char.toLowerCase();
    let baseDelay = this.TYPE_SPEED_BASE;

    // Adjust base delay based on character
    if (this.FAST_CHARS.has(lowerChar)) {
      baseDelay *= 0.7; // Faster for common/easy chars
    } else if (this.SLOW_CHARS.has(lowerChar)) {
      baseDelay *= 1.4; // Slower for uncommon/harder chars
    }

    // Add pause after spaces (natural word break)
    if (char === ' ') {
      baseDelay += Math.random() * 60;
    }

    // Random variance for organic feel
    const variance = (Math.random() - 0.5) * 2 * this.TYPE_SPEED_VARIANCE;

    // Occasional micro-pause (5% chance) - simulates brief hesitation
    const microPause = Math.random() < 0.05 ? 80 : 0;

    return Math.max(15, baseDelay + variance + microPause);
  }

  /**
   * Calculate deletion delay - faster and more consistent (like holding backspace)
   */
  private getDeletingDelay(): number {
    const variance = (Math.random() - 0.5) * 2 * this.DELETE_SPEED_VARIANCE;
    return Math.max(10, this.DELETE_SPEED_BASE + variance);
  }

  private scheduleNextTick(delay: number): void {
    this.animationTimer = setTimeout(() => this.tick(), delay);
  }

  private selectNextPhrase(): void {
    // Select a random phrase different from the current one
    let nextIndex: number;
    do {
      nextIndex = Math.floor(Math.random() * LOADING_PHRASES.length);
    } while (nextIndex === this.currentPhraseIndex() && LOADING_PHRASES.length > 1);

    this.currentPhraseIndex.set(nextIndex);
  }
}
