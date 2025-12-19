import { Component, input, ChangeDetectionStrategy } from '@angular/core';

/**
 * ThinkingDotsComponent
 *
 * A sophisticated loading indicator featuring animated dots with wave motion.
 * Dots animate vertically with a wave pattern using the primary color.
 * The component uses signal-based inputs for flexibility and OnPush change
 * detection for performance.
 *
 * @example
 * ```html
 * <app-thinking-dots />
 * <app-thinking-dots [label]="'Generating response'" [size]="'md'" />
 * ```
 */
@Component({
  selector: 'app-thinking-dots',
  template: `
    <div
      class="flex flex-col items-center justify-center gap-6"
      [attr.aria-busy]="true"
      role="status"
    >
      <!-- Animated dots container -->
      <div
        class="thinking-dots-container"
        [class]="containerSizeClass()"
      >
        <div
          class="thinking-dot"
          [class]="dotSizeClass()"
          style="animation-delay: 0s;"
        ></div>
        <div
          class="thinking-dot"
          [class]="dotSizeClass()"
          style="animation-delay: 0.15s;"
        ></div>
        <div
          class="thinking-dot"
          [class]="dotSizeClass()"
          style="animation-delay: 0.3s;"
        ></div>
      </div>

      <!-- Optional label -->
      @if (label()) {
        <p
          class="text-center text-sm font-medium text-gray-600 dark:text-gray-400"
          [class]="labelSizeClass()"
        >
          {{ label() }}
        </p>
      }
    </div>

    <style>
      .thinking-dots-container {
        display: flex;
        align-items: center;
      }

      .thinking-dot {
        border-radius: 50%;
        background: var(--color-primary-500);
        animation: thinking-wave 1.4s ease-in-out infinite;
      }

      @keyframes thinking-wave {
        0%,
        60%,
        100% {
          transform: translateY(0) scale(1);
        }
        30% {
          transform: translateY(-12px) scale(1.1);
        }
      }

      /* Size variants - gap spacing */
      .thinking-dots-container.gap-sm {
        gap: 6px;
      }

      .thinking-dots-container.gap-md {
        gap: 8px;
      }

      .thinking-dots-container.gap-lg {
        gap: 10px;
      }

      /* Size-specific animations for proportional movement */
      .thinking-dot.size-2 {
        animation-name: thinking-wave-sm;
      }

      .thinking-dot.size-4 {
        animation-name: thinking-wave-lg;
      }

      @keyframes thinking-wave-sm {
        0%,
        60%,
        100% {
          transform: translateY(0) scale(1);
        }
        30% {
          transform: translateY(-8px) scale(1.1);
        }
      }

      @keyframes thinking-wave-lg {
        0%,
        60%,
        100% {
          transform: translateY(0) scale(1);
        }
        30% {
          transform: translateY(-16px) scale(1.1);
        }
      }
    </style>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ThinkingDotsComponent {
  /**
   * Optional label text displayed below the dots
   * @default undefined
   */
  label = input<string | undefined>(undefined);

  /**
   * Size variant: 'sm' | 'md' | 'lg'
   * @default 'md'
   */
  size = input<'sm' | 'md' | 'lg'>('md');

  /**
   * Returns the appropriate container size class based on the size input
   */
  containerSizeClass = () => {
    const sizeMap = {
      sm: 'gap-sm',
      md: 'gap-md',
      lg: 'gap-lg',
    };
    return sizeMap[this.size()];
  };

  /**
   * Returns the appropriate dot size class based on the size input
   */
  dotSizeClass = () => {
    const sizeMap = {
      sm: 'size-2',
      md: 'size-3',
      lg: 'size-4',
    };
    return sizeMap[this.size()];
  };

  /**
   * Returns the appropriate label size class based on the size input
   */
  labelSizeClass = () => {
    const sizeMap = {
      sm: 'text-xs',
      md: 'text-sm',
      lg: 'text-base',
    };
    return sizeMap[this.size()];
  };
}
