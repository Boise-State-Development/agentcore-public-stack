import { Component, ChangeDetectionStrategy, input, signal, effect, OnDestroy } from '@angular/core';

@Component({
  selector: 'app-animated-text',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h1 class="text-4xl/tight font-semibold text-gray-900 dark:text-gray-100">
      {{ displayedText() }}@if (isAnimating()) {<span class="animate-pulse">|</span>}
    </h1>
  `,
  styles: [`
    @keyframes pulse {
      0%, 100% {
        opacity: 1;
      }
      50% {
        opacity: 0;
      }
    }
    .animate-pulse {
      animation: pulse 1s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }
  `]
})
export class AnimatedTextComponent implements OnDestroy {
  // Input text to animate
  text = input.required<string>();

  // Animation speed in milliseconds per character
  speed = input<number>(50);

  // Whether to show the cursor
  showCursor = input<boolean>(true);

  // The currently displayed text
  displayedText = signal('');

  // Whether the animation is currently running
  isAnimating = signal(true);

  private animationTimeoutId?: number;

  constructor() {
    // Effect to trigger animation when text input changes
    effect(() => {
      const fullText = this.text();
      const animationSpeed = this.speed();

      // Clear any existing animation
      if (this.animationTimeoutId) {
        clearTimeout(this.animationTimeoutId);
      }

      // Reset displayed text and show cursor
      this.displayedText.set('');
      this.isAnimating.set(true);

      // Animate character by character
      this.animateText(fullText, 0, animationSpeed);
    });
  }

  ngOnDestroy(): void {
    if (this.animationTimeoutId) {
      clearTimeout(this.animationTimeoutId);
    }
  }

  private animateText(fullText: string, currentIndex: number, speed: number): void {
    if (currentIndex < fullText.length) {
      this.displayedText.update(text => text + fullText[currentIndex]);

      this.animationTimeoutId = window.setTimeout(() => {
        this.animateText(fullText, currentIndex + 1, speed);
      }, speed);
    } else {
      // Animation complete - hide cursor
      this.isAnimating.set(false);
    }
  }
}
