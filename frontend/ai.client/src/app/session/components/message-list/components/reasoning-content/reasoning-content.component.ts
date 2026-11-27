import {
  Component,
  ElementRef,
  afterRenderEffect,
  effect,
  inject,
  input,
  signal,
  computed,
  untracked,
  viewChild,
  ChangeDetectionStrategy,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { ContentBlock, ReasoningContentData } from '../../../../services/models/message.model';
import { formatThinkingDuration, latestCompleteSentence } from './reasoning-text';

let nextPanelId = 0;

/** The most the one-line preview beside the headline shows. */
const PREVIEW_MAX_CHARS = 80;

/** How close to the end of the live box still counts as reading the end: about a line. */
const FOLLOW_TAIL_SLACK_PX = 24;

/**
 * The model's reasoning, drawn as a sibling of the tool rail rather than a
 * card of its own: the same chevron headline, the same muted type, the same
 * sweep while it is still going, and the same bordered, scrolling box when
 * opened. Reasoning and tool calls are both "what the agent did on the way
 * to the answer", and they sit one above the other in a turn, so they should
 * read as one family. The headline group is NAMED (`group/reasoning`) for
 * the reason the rail's is: the message card is itself a `group`.
 *
 * While the model is still thinking the box opens on its own and follows the
 * text as it streams, so the seconds before the answer are spent reading
 * rather than waiting. It closes again the moment the model starts answering.
 * A reloaded conversation never opens it: only a block that is streaming does.
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

    <!-- Says that thinking started and stopped, and nothing else. The text
         itself is not a live region: it changes several times a second, and a
         screen reader would read the whole of it over the answer. It is one
         Tab away in the box below, which opens without taking focus. -->
    <span class="sr-only" role="status">{{ announcement() }}</span>

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
            <!-- Shorter while it streams: it is a glimpse of work in progress
                 above an answer that has not started, not the whole record. -->
            <div
              #scrollBox
              class="reasoning-scroll bg-white/40 px-2.5 py-2 text-xs/5 text-gray-600 dark:bg-transparent dark:text-gray-300"
              [class.max-h-48]="isThinking()"
              [class.max-h-80]="!isThinking()"
              tabindex="0"
              role="group"
              aria-label="Reasoning"
              (scroll)="onBoxScroll(scrollBox)"
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

    /* The box opens and closes on its own during a turn, so it must not
       slide for someone who asked for less motion. */
    @media (prefers-reduced-motion: reduce) {
      .chevron,
      .collapsible-content,
      .collapsible-content > div,
      .collapsible-content.open > div {
        transition: none;
      }
    }
  `
})
export class ReasoningContentComponent {
  /** The content block containing reasoning data */
  contentBlock = input.required<ContentBlock>();

  /** True while this block belongs to the message that is still streaming. */
  isStreaming = input(false);

  protected readonly panelId = `reasoning-panel-${nextPanelId++}`;

  private readonly document = inject(DOCUMENT);
  private readonly scrollBox = viewChild<ElementRef<HTMLElement>>('scrollBox');

  /** What the user chose by clicking the headline; null until they have. */
  private readonly userExpanded = signal<boolean | null>(null);

  /** This block was seen thinking by this component, so it is not a reload. */
  private readonly streamedHere = signal(false);

  /**
   * Whether the live box is still following the end of the text. Cleared when
   * the user scrolls back to read something, so the stream stops pulling the
   * text out from under them.
   */
  private followTail = true;

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
   * The box opens itself: the model is thinking and has written more than the
   * one line beside the headline can hold. A thought that fits on that line is
   * already fully visible there, so the box does not flash open and shut for
   * the one-sentence thinking most short questions get.
   */
  protected readonly isLive = computed(
    () => this.isThinking() && this.reasoningText().length > PREVIEW_MAX_CHARS,
  );

  /** Open when the user chose so, otherwise only while the box is live. */
  readonly isExpanded = computed(() => this.userExpanded() ?? this.isLive());

  /**
   * The polite announcement: thinking started, then how long it took. Empty
   * for a block that never streamed here, so a reloaded conversation is not
   * read out block by block.
   */
  protected readonly announcement = computed(() => {
    if (!this.streamedHere()) return '';
    if (this.isThinking()) return 'Thinking';
    const elapsed = this.durationLabel();
    return elapsed ? `Thought for ${elapsed}` : '';
  });

  constructor() {
    // Thinking started or stopped. On the stop, someone who scrolled back in
    // the box or put focus in it is reading it, so it stays open for them
    // instead of closing as the answer arrives.
    let wasThinking = false;
    let wasOpen = false;
    effect(() => {
      const thinking = this.isThinking();
      const open = this.isExpanded();
      if (thinking) {
        wasThinking = true;
        wasOpen = open;
        this.streamedHere.set(true);
        return;
      }
      if (!wasThinking) return;
      wasThinking = false;
      if (wasOpen && untracked(this.userExpanded) === null && this.readerIsInTheBox()) {
        this.userExpanded.set(true);
      }
    });

    // Keep the newest line in view while the box is live. Runs after the
    // render that painted the delta, so `scrollHeight` already includes it.
    // Setting `scrollTop` jumps; nothing here animates.
    afterRenderEffect(() => {
      this.reasoningText();
      const box = this.scrollBox()?.nativeElement;
      if (!box || !this.isLive() || !this.followTail) return;
      box.scrollTop = box.scrollHeight;
    });
  }

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

  /** The duration formatted for the header, or null when there isn't one. */
  protected readonly durationLabel = computed<string | null>(() =>
    formatThinkingDuration(this.contentBlock().reasoningDurationMs),
  );

  /**
   * The one line beside the headline.
   *
   * While the model is thinking it is the latest COMPLETE sentence, so the
   * line says what the model is on now rather than how it started, and moves
   * a sentence at a time instead of flickering word by word as deltas land.
   * Until a sentence completes it is the opening, as is a finished or
   * reloaded block, where the opening is the better summary of the whole.
   */
  previewText = computed(() => {
    const text = this.reasoningText();
    const raw = this.reasoningData()?.reasoningText?.text ?? '';
    const line = (this.isThinking() && latestCompleteSentence(raw)) || text;
    return line.length <= PREVIEW_MAX_CHARS ? line : line.substring(0, PREVIEW_MAX_CHARS) + '...';
  });

  /** Toggle expanded state. From here on the user's choice stands. */
  toggleExpanded(): void {
    this.userExpanded.set(!this.isExpanded());
  }

  /** The user (or the follow itself) scrolled the box. */
  protected onBoxScroll(box: HTMLElement): void {
    this.followTail = box.scrollHeight - box.scrollTop - box.clientHeight <= FOLLOW_TAIL_SLACK_PX;
  }

  private readerIsInTheBox(): boolean {
    const box = this.scrollBox()?.nativeElement;
    if (!box) return false;
    return !this.followTail || box.contains(this.document.activeElement);
  }
}
