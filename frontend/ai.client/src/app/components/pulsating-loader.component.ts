import {
  Component,
  ChangeDetectionStrategy,
  signal,
  computed,
  input,
  OnInit,
  OnDestroy,
  inject,
  PLATFORM_ID,
} from '@angular/core';
import { isPlatformBrowser } from '@angular/common';

/**
 * PulsatingLoaderComponent
 *
 * The line shown while a turn is running: a small pulsing dot, what the agent
 * is doing, and how long it has been doing it.
 *
 * WHAT THIS DELIBERATELY NO LONGER DOES
 * -------------------------------------
 * It used to cycle twenty invented phrases — "Pondering", "Cross-referencing",
 * "Consulting the archives" — typed out character by character. They were
 * charming and they were fiction: identical whether the model was generating,
 * waiting on a Canvas round trip, or hung. A user watching "Cross-referencing"
 * for ninety seconds learned nothing, and two of those ninety-second turns got
 * abandoned in prod.
 *
 * Everything shown here is now a fact we actually hold:
 *
 * - `status` comes from the runtime's `agent_status` events — the event loop's
 *   own model-call and tool-call boundaries.
 * - `statusTool` is the tool's real name. While a tool runs, its name is the
 *   most accurate label available and invents nothing.
 * - the elapsed timer is measured from the moment the turn was sent.
 *
 * `notice` outranks both. It states a specific fact that is NOT the healthy
 * path (the model is being retried), so it takes the warning colour and the
 * amber dot — the dot matters because it is the part a user tracks
 * peripherally, and changing only the text leaves the indicator looking
 * routine during an outage.
 */
@Component({
  selector: 'app-pulsating-loader',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="flex items-center gap-2"
      role="status"
      [attr.aria-busy]="true"
      aria-live="polite"
      [attr.aria-label]="ariaLabel()"
    >
      <span class="pulse-dot" [class.is-notice]="!!notice()" aria-hidden="true"></span>

      <span class="sep" aria-hidden="true">&bull;</span>

      <!-- Tabular figures so the seconds tick without the line jittering, and
           aria-hidden so a screen reader is not re-announced to every second. -->
      @if (elapsedLabel(); as elapsed) {
        <span class="text-xs tabular-nums text-gray-400 dark:text-gray-500" aria-hidden="true">{{ elapsed }}</span>
        <span class="sep" aria-hidden="true">&bull;</span>
      }

      <!-- Tracked by its own text, so a change in state destroys this node and
           builds a new one — which is what lets the enter animation run on
           every transition. The timer is a sibling for the same reason in
           reverse: it must NOT re-animate once a second. -->
      @for (frame of stateFrames(); track frame) {
        <span
          class="state text-sm"
          [class.is-notice]="!!notice()"
          [class.shimmer]="!notice()"
        >
          {{ label() }}
          @if (statusTool(); as tool) {
            <span class="font-mono text-[13px]">{{ tool }}</span>
          }
        </span>
      }
    </div>

    <style>
      :host {
        display: block;
      }

      /*
       * 7px, opacity-only. The previous indicator was a 12px dot inside a 36px
       * expanding ring, which drew more attention than the sentence next to it.
       */
      .pulse-dot {
        width: 7px;
        height: 7px;
        border-radius: 9999px;
        flex: none;
        background-color: var(--color-secondary-500);
        animation: loader-pulse 1.3s ease-in-out infinite;
      }

      .pulse-dot.is-notice {
        background-color: var(--color-state-warning-500);
      }

      :host-context(.dark) .pulse-dot {
        background-color: var(--color-secondary-400);
      }

      .sep {
        font-size: 11px;
        line-height: 1;
        color: var(--color-gray-400);
      }

      :host-context(.dark) .sep {
        color: var(--color-gray-600);
      }

      .state {
        --shimmer-base: #6b7280;      /* gray-500 */
        --shimmer-highlight: #d1d5db; /* gray-300 */
        background: linear-gradient(
          90deg,
          var(--shimmer-base) 25%,
          var(--shimmer-highlight) 50%,
          var(--shimmer-base) 75%
        );
        background-size: 200% 100%;
        background-clip: text;
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        animation: state-enter 0.3s ease-out;
      }

      :host-context(.dark) .state {
        --shimmer-base: #9ca3af;      /* gray-400 */
        --shimmer-highlight: #f3f4f6; /* gray-100 */
      }

      .state.shimmer {
        animation:
          state-enter 0.3s ease-out,
          loader-shimmer 2.2s ease-in-out infinite;
      }

      /* The mono tool name is a child, so it must inherit the gradient rather
         than paint its own colour over it. */
      .state span {
        color: inherit;
        -webkit-text-fill-color: inherit;
      }

      /*
       * A notice is a warning, and a warning that shimmers reads as decoration.
       * It opts out of the gradient entirely and keeps a solid amber.
       */
      .state.is-notice {
        background: none;
        -webkit-text-fill-color: currentColor;
        color: var(--color-state-warning-700);
      }

      :host-context(.dark) .state.is-notice {
        color: var(--color-state-warning-400);
      }

      @keyframes loader-pulse {
        0%,
        100% {
          opacity: 0.35;
          transform: scale(0.8);
        }
        50% {
          opacity: 1;
          transform: scale(1);
        }
      }

      @keyframes loader-shimmer {
        0% {
          background-position: 200% center;
        }
        100% {
          background-position: -200% center;
        }
      }

      @keyframes state-enter {
        from {
          opacity: 0;
          transform: translateY(3px);
        }
        to {
          opacity: 1;
          transform: translateY(0);
        }
      }

      @media (prefers-reduced-motion: reduce) {
        .pulse-dot {
          animation: none;
          opacity: 0.8;
        }

        .state,
        .state.shimmer {
          animation: none;
        }
      }
    </style>
  `,
})
export class PulsatingLoaderComponent implements OnInit, OnDestroy {
  private platformId = inject(PLATFORM_ID);

  /**
   * A specific fact that is not the healthy path — currently only "the model
   * is being retried". Outranks `status`: a retry in progress is the more
   * important truth.
   */
  notice = input<string | null>(null);

  /**
   * What the agent is doing, from `agent_status`. Null falls back to the
   * generic waiting label.
   */
  status = input<string | null>(null);

  /**
   * The running tool's own name, rendered in mono beside `status`. Separate
   * from `status` so the identifier is visibly an identifier.
   */
  statusTool = input<string | null>(null);

  /**
   * Epoch ms the turn started. Null hides the timer entirely rather than
   * showing a zero that never moves.
   */
  startedAt = input<number | null>(null);

  /** Ticks once a second so the elapsed readout recomputes. */
  private readonly now = signal(Date.now());
  private timer: ReturnType<typeof setInterval> | null = null;

  /**
   * Falls back to "Thinking" rather than a vaguer word.
   *
   * Before the first `agent_status` arrives there is a real gap — the request
   * in flight, the session loading, the agent building — and we cannot tell
   * those apart. But from the user's side every one of them is the same fact:
   * the assistant has the turn and has not answered yet. "Thinking" states
   * that; "Working" was a hedge that said less and, on a cold start, was the
   * only thing shown for the first several seconds.
   */
  protected readonly label = computed(
    () => this.notice() ?? this.status() ?? 'Thinking',
  );

  protected readonly elapsedLabel = computed(() => {
    const started = this.startedAt();
    if (!started) return null;
    const seconds = Math.max(0, Math.floor((this.now() - started) / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${seconds % 60}s`;
  });

  /**
   * A single frame keyed by the visible text.
   *
   * `@for ... track frame` over this is what animates the state change: when
   * the text differs the old node is destroyed and a new one created, so the
   * enter keyframe runs. A plain interpolation would mutate the text in place
   * and never animate. The timer stays outside this loop deliberately — it
   * changes every second and must not re-animate.
   */
  protected readonly stateFrames = computed(() => {
    const tool = this.statusTool();
    return [tool ? `${this.label()} ${tool}` : this.label()];
  });

  /**
   * The timer is `aria-hidden` and re-announced text would be noise, so the
   * accessible name carries the state only.
   */
  protected readonly ariaLabel = computed(() => {
    const tool = this.statusTool();
    return tool ? `${this.label()} ${tool}` : this.label();
  });

  ngOnInit(): void {
    // No interval during SSR: it would never fire and would keep the platform
    // from stabilising.
    if (!isPlatformBrowser(this.platformId)) return;
    this.timer = setInterval(() => this.now.set(Date.now()), 1000);
  }

  ngOnDestroy(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
