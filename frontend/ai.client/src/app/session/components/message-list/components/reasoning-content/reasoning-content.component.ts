import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { ContentBlock, ReasoningContentData } from '../../../../services/models/message.model';

let nextPanelId = 0;

/**
 * The model's reasoning, drawn as a sibling of the tool rail rather than a
 * card of its own: the same chevron headline, the same muted type, the same
 * sweep while it is still going, and the same bordered, scrolling box when
 * opened. Reasoning and tool calls are both "what the agent did on the way
 * to the answer", and they sit one above the other in a turn, so they should
 * read as one family. The headline group is NAMED (`group/reasoning`) for
 * the reason the rail's is: the message card is itself a `group`.
 */
@Component({
  selector: 'app-reasoning-content',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  template: `
    <button
      type="button"
      class="group/reasoning flex w-full cursor-pointer items-center gap-2 text-left"
      [attr.aria-expanded]="isExpanded()"
      [attr.aria-controls]="panelId"
      (click)="toggleExpanded()"
    >
      <svg
        class="chevron size-3 shrink-0 text-gray-400 dark:text-gray-500"
        [class.open]="isExpanded()"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
      </svg>

      <span
        class="reasoning-headline shrink-0 text-xs text-gray-500 group-hover/reasoning:text-gray-700 dark:text-gray-400 dark:group-hover/reasoning:text-gray-200"
        [class.text-shimmer]="isThinking()"
      >{{ headerLabel() }}</span>

      <!-- The preview is secondary metadata beside the headline, styled like
           the rail's counts and timings, and gone once the full text is open. -->
      @if (!isExpanded() && hasReasoningText()) {
        <span class="min-w-0 truncate text-[11px] text-gray-400 dark:text-gray-500">{{ previewText() }}</span>
      }

      @if (hasRedactedContent()) {
        <span class="shrink-0 text-[11px] font-medium text-state-warning-700 dark:text-state-warning-300">
          Redacted
        </span>
      }
    </button>

    <!-- Collapsed content is inert: a 0fr row clips it from view but would
         still put the scroll box in the tab order. -->
    <div
      class="collapsible-content"
      [id]="panelId"
      [class.open]="isExpanded()"
      [attr.inert]="isExpanded() ? null : ''"
    >
      <div>
        <div class="mt-2 overflow-hidden rounded-lg border border-gray-200/80 dark:border-white/10">
          @if (hasReasoningText()) {
            <div
              class="reasoning-scroll max-h-80 bg-white/40 px-2.5 py-2 text-xs/5 text-gray-600 dark:bg-transparent dark:text-gray-300"
              tabindex="0"
              role="group"
              aria-label="Reasoning"
            >{{ reasoningText() }}</div>
          }

          @if (hasRedactedContent()) {
            <div
              class="flex items-center gap-1.5 bg-gray-100/70 px-2.5 py-1.5 text-[11px] font-medium text-state-warning-700 dark:bg-white/5 dark:text-state-warning-300"
              [class.border-t]="hasReasoningText()"
              [class.border-gray-200/80]="hasReasoningText()"
              [class.dark:border-white/10]="hasReasoningText()"
            >
              <ng-icon name="heroExclamationTriangle" class="size-3.5 shrink-0" aria-hidden="true" />
              <span>Some reasoning content was redacted for safety purposes.</span>
            </div>
          }
        </div>
      </div>
    </div>
  `,
  styles: `
    :host {
      display: block;
    }

    /* Chevron and accordion match the tool rail (tool-rail.component.css). */
    .chevron {
      transition: transform 0.2s ease-out;
    }

    .chevron.open {
      transform: rotate(90deg);
    }

    .collapsible-content {
      display: grid;
      grid-template-rows: 0fr;
      transition: grid-template-rows 0.2s ease-out;
    }

    .collapsible-content.open {
      grid-template-rows: 1fr;
    }

    .collapsible-content > div {
      overflow: hidden;
      opacity: 0;
      transition: opacity 0s ease-out;
    }

    .collapsible-content.open > div {
      opacity: 1;
      transition: opacity 0.15s ease-out 0.05s;
    }

    /* Prose, not a payload, so it keeps the sans face the rail's row labels
       use; it scrolls inside the box rather than pushing the answer down. */
    .reasoning-scroll {
      overflow-y: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .reasoning-scroll:focus-visible {
      outline: 2px solid var(--color-primary-500);
      outline-offset: -2px;
    }
  `
})
export class ReasoningContentComponent {
  /** The content block containing reasoning data */
  contentBlock = input.required<ContentBlock>();

  /** True while this block belongs to the message that is still streaming. */
  isStreaming = input(false);

  protected readonly panelId = `reasoning-panel-${nextPanelId++}`;

  /** Whether the reasoning content is expanded */
  isExpanded = signal(false);

  /** Extract reasoning content data from the content block */
  reasoningData = computed(() => {
    const block = this.contentBlock();
    return block.reasoningContent as ReasoningContentData | null;
  });

  /** The reasoning text, trimmed: the box is `pre-wrap`, so a trailing
   *  newline from the model would render as a blank last line. */
  reasoningText = computed(() => {
    const data = this.reasoningData();
    return (data?.reasoningText?.text || '').trim();
  });

  /** Check if there is reasoning text to display */
  hasReasoningText = computed(() => {
    return this.reasoningText().length > 0;
  });

  /** Check if content was redacted for safety */
  hasRedactedContent = computed(() => {
    const data = this.reasoningData();
    return !!data?.redactedContent;
  });

  /**
   * The model is still thinking: the message is streaming and the stream
   * parser has not yet closed the span with a duration. Drives the sweep.
   */
  protected readonly isThinking = computed(
    () => this.isStreaming() && typeof this.contentBlock().reasoningDurationMs !== 'number',
  );

  /**
   * The header: "Thinking" while the model is still at it, "Thought for 17s"
   * once it has stopped.
   *
   * The duration is live-only — the stream parser measures it, `GET /messages`
   * never carries it — so a reloaded conversation reads "Thinking" in the past
   * tense sense of a collapsed section title. That is the same trade the tool
   * rail makes with its durations: no number beats a number nobody measured.
   * See docs/specs/agent-state-feedback.md PR-1.
   */
  protected readonly headerLabel = computed(() => {
    const elapsed = this.durationLabel();
    return elapsed ? `Thought for ${elapsed}` : 'Thinking';
  });

  /**
   * The duration formatted for the header, or null when there isn't one.
   *
   * Sub-second blocks read "<1s" rather than rounding up to "1s": the whole
   * point of the readout is that it is measured, and 400ms is not a second.
   */
  protected readonly durationLabel = computed<string | null>(() => {
    const ms = this.contentBlock().reasoningDurationMs;
    if (typeof ms !== 'number' || ms < 0) return null;
    if (ms < 1000) return '<1s';

    const seconds = Math.round(ms / 1000);
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  });

  /** Get a preview of the reasoning text (first 80 chars) */
  previewText = computed(() => {
    const text = this.reasoningText();
    if (text.length <= 80) return text;
    return text.substring(0, 80) + '...';
  });

  /** Toggle expanded state */
  toggleExpanded(): void {
    this.isExpanded.update(expanded => !expanded);
  }
}
