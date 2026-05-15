import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBoxArrowDown } from '@ng-icons/heroicons/outline';

/**
 * Single subtle indicator rendered at the end of the conversation when
 * any context compaction has happened in this session. Replaces the
 * earlier per-message inline divider, which caused jarring layout shifts
 * when it dropped between messages mid-conversation. Hover/focus reveals
 * the cumulative turn count in a tooltip.
 *
 * `animate` plays a one-shot fade-in only on the first appearance
 * (0 → >0 transition). Subsequent updates to `summarizedTurns` change
 * the tooltip in place with no animation.
 */
@Component({
  selector: 'app-compaction-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArchiveBoxArrowDown })],
  styles: `
    :host {
      display: block;
    }
    @keyframes compactionFadeIn {
      from {
        opacity: 0;
        transform: translateY(2px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    .compaction-enter {
      animation: compactionFadeIn 350ms ease-out backwards;
    }
  `,
  template: `
    <div
      role="status"
      [attr.aria-label]="ariaLabel()"
      class="mt-2 flex justify-start text-[11px] text-gray-400 dark:text-gray-500"
      [class.compaction-enter]="animate()"
    >
      <span
        class="group relative inline-flex items-center gap-1 rounded-sm px-1 py-0.5 outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900"
        tabindex="0"
      >
        <ng-icon
          name="heroArchiveBoxArrowDown"
          class="text-xs text-gray-400 dark:text-gray-500"
          aria-hidden="true"
        />
        <span>Earlier messages summarized</span>

        <!-- Hover/focus tooltip -->
        <span
          role="tooltip"
          class="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 w-64 -translate-x-1/2 rounded-md border border-gray-200 bg-white p-3 text-left opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 dark:border-gray-700 dark:bg-gray-800"
        >
          <span
            class="block text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400"
          >
            Context summarized
          </span>
          <span class="mt-1 block text-xs text-gray-700 dark:text-gray-300">
            {{ summaryDetail() }}
          </span>
          <span
            class="mt-2 block text-[11px] leading-snug text-gray-500 dark:text-gray-400"
          >
            Older turns were condensed into a summary so the model can keep
            responding without losing track of the conversation.
          </span>
        </span>
      </span>
    </div>
  `,
})
export class CompactionSummaryComponent {
  /** Cumulative count of turns summarized across all compaction events
   *  in this session. */
  summarizedTurns = input.required<number>();

  /** Whether to play the one-shot fade-in animation on first render. */
  animate = input<boolean>(false);

  protected readonly summaryDetail = computed(() => {
    const turns = this.summarizedTurns();
    return `${turns.toLocaleString()} earlier ${turns === 1 ? 'turn has' : 'turns have'} been summarized to keep context manageable.`;
  });

  protected readonly ariaLabel = computed(() => {
    const turns = this.summarizedTurns();
    return `${turns} earlier ${turns === 1 ? 'turn' : 'turns'} summarized to save context`;
  });
}
