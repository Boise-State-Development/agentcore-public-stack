import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
  ElementRef,
  viewChild,
  AfterViewInit,
  OnDestroy,
  inject,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowTurnDownRight } from '@ng-icons/heroicons/outline';
import { ContentBlock, Message, FileAttachmentData } from '../../../services/models/message.model';
import { FileAttachmentBadgeComponent, ImageAttachmentGroupComponent } from './file-attachment';
import { MentionTextComponent } from './mention-text.component';
import { LocalSettingsService } from '../../../../services/local-settings.service';
import { parseIso } from '../../../../utils/date';

function isImageMimeType(mimeType: string): boolean {
  return mimeType.startsWith('image/');
}

const MAX_HEIGHT_PX = 200;

@Component({
  selector: 'app-user-message',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FileAttachmentBadgeComponent, ImageAttachmentGroupComponent, MentionTextComponent, NgIcon],
  viewProviders: [provideIcons({ heroArrowTurnDownRight })],
  template: `
    @if (hasTextContent() || hasFileAttachments()) {
      <div class="group relative flex w-full flex-col items-end gap-2">
        <!-- Hover-revealed sent-at subtitle (positioned above the topmost slot) -->
        @if (formattedSentAt()) {
          <span
            class="pointer-events-none absolute -top-5 right-1 text-xs text-gray-400 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 motion-reduce:transition-none dark:text-gray-500"
            aria-hidden="true"
          >
            {{ formattedSentAt() }}
          </span>
        }

        <!--
          Mid-turn steer: the user sent this INTO a response that was still
          streaming, and the agent picked it up at its next step. Labelled
          because the bubble alone reads as the start of a new turn, which is
          exactly what it isn't — the reply above and below it is one response.
        -->
        @if (message().steering) {
          <span class="flex items-center gap-1 pr-1 text-xs text-gray-500 dark:text-gray-400">
            <ng-icon name="heroArrowTurnDownRight" class="size-3.5" aria-hidden="true" />
            Sent while responding
          </span>
        }

        <!-- Text content (message bubble) -->
        @if (hasTextContent()) {
          <!--
            A mid-turn steer uses the SAME bubble as any other user message.
            It IS an ordinary thing the user said; only its timing is unusual,
            and the "Sent while responding" caption above already carries that.
            A second visual treatment made it read as a different kind of
            object, which it isn't.
          -->
          <div
            class="max-w-[80%] rounded-2xl bg-primary-500 px-4 py-3 text-base/6 text-white/90"
          >
            <div class="relative">
              <div
                #contentWrapper
                class="overflow-hidden transition-[max-height] duration-300 ease-in-out"
                [style.max-height]="expanded() ? 'none' : maxHeightPx + 'px'"
              >
                @if (displayText(); as text) {
                  <p class="whitespace-pre-wrap"><app-mention-text [text]="text" /></p>
                } @else {
                  @for (block of message().content; track $index) {
                    @if (block.type === 'text' && block.text) {
                      <p class="whitespace-pre-wrap"><app-mention-text [text]="block.text" /></p>
                    }
                  }
                }
              </div>
              @if (isOverflowing() && !expanded()) {
                <div
                  class="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-primary-500 to-transparent"
                ></div>
              }
            </div>
            @if (isOverflowing()) {
              <button
                type="button"
                (click)="toggleExpanded()"
                class="mt-2 text-sm font-medium text-white/80 underline underline-offset-2 hover:text-white"
              >
                {{ expanded() ? 'Show less' : 'Show more' }}
              </button>
            }
          </div>
        }

        <!-- Image attachments (iMessage-style mosaic) -->
        @if (imageAttachments().length > 0) {
          <div class="flex max-w-[80%] justify-end">
            <app-image-attachment-group [attachments]="imageAttachments()" />
          </div>
        }

        <!-- Non-image file attachments (below message bubble) -->
        @if (nonImageAttachments().length > 0) {
          <div class="flex max-w-[80%] flex-wrap justify-end gap-2">
            @for (attachment of nonImageAttachments(); track attachment.uploadId) {
              <app-file-attachment-badge [attachment]="attachment" />
            }
          </div>
        }
      </div>
    }
  `,
  styles: `
    :host {
      display: block;
    }
  `,
})
export class UserMessageComponent implements AfterViewInit, OnDestroy {
  message = input.required<Message>();

  contentWrapper = viewChild<ElementRef<HTMLDivElement>>('contentWrapper');

  expanded = signal(false);
  isOverflowing = signal(false);

  private localSettings = inject(LocalSettingsService);

  readonly maxHeightPx = MAX_HEIGHT_PX;

  /** Original user message before prompt modification — skipped when debug output is enabled */
  displayText = computed((): string | null => {
    if (this.localSettings.showDebugOutput()) return null;
    const metadata = this.message().metadata;
    if (metadata && typeof metadata['displayText'] === 'string') {
      return metadata['displayText'];
    }
    return null;
  });

  /**
   * Hover-revealed "sent at" subtitle rendered above the topmost message slot.
   *
   * - Under 1 minute: "Just now"
   * - Under 1 hour: "{n}m ago"
   * - Under 24 hours: "{n}h ago"
   * - 24 hours or older: full localized date and time
   * - Missing/unparseable timestamp: "" (the subtitle slot collapses out)
   *
   * Note: this is a `computed()` keyed off `message().createdAt`, so the
   * relative label is captured at render time and won't tick forward while
   * a session sits idle. Streaming and message updates re-render the list,
   * which is when the relative value refreshes for active conversations.
   */
  formattedSentAt = computed((): string => {
    const createdAt = this.message().createdAt;
    if (!createdAt) return '';

    const sentMs = Date.parse(createdAt);
    if (Number.isNaN(sentMs)) return '';

    const diffMs = Date.now() - sentMs;
    const diffMinutes = Math.floor(diffMs / 60_000);
    const diffHours = Math.floor(diffMs / 3_600_000);

    if (diffMs < 60_000) return 'Just now';
    if (diffMinutes < 60) return `${diffMinutes}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;

    return parseIso(sentMs).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  });

  hasTextContent = computed(() => {
    if (this.displayText()) return true;
    return this.message().content.some(
      (block: ContentBlock) => block.type === 'text' && block.text
    );
  });

  hasFileAttachments = computed(() => {
    return this.message().content.some(
      (block: ContentBlock) => block.type === 'fileAttachment' && block.fileAttachment
    );
  });

  fileAttachments = computed((): FileAttachmentData[] => {
    return this.message().content
      .filter((block: ContentBlock) => block.type === 'fileAttachment' && block.fileAttachment)
      .map((block: ContentBlock) => block.fileAttachment as FileAttachmentData);
  });

  imageAttachments = computed((): FileAttachmentData[] =>
    this.fileAttachments().filter((a) => isImageMimeType(a.mimeType)),
  );

  nonImageAttachments = computed((): FileAttachmentData[] =>
    this.fileAttachments().filter((a) => !isImageMimeType(a.mimeType)),
  );

  /** Re-measures whenever the bubble's box settles. See {@link ngAfterViewInit}. */
  private resizeObserver?: ResizeObserver;

  ngAfterViewInit(): void {
    const wrapper = this.contentWrapper()?.nativeElement;
    if (!wrapper) {
      return;
    }

    // Measure whenever the box settles, not once and for all.
    //
    // A single `ngAfterViewInit` reading can land before layout has settled,
    // and it latched: a one-line mid-turn steer measured its 40 characters as
    // dozens of wrapped lines in a not-yet-widened container, set
    // `isOverflowing`, and rendered a "Show more" plus a fade dimming its own
    // single line — permanently, because nothing ever measured again.
    // Observed live on dev.
    //
    // The observed element is the inner wrapper, whose box is pinned by
    // `max-height` and unaffected by the fade (absolutely positioned) and the
    // button (a sibling outside it), so reacting here cannot feed back into a
    // resize loop.
    this.resizeObserver = new ResizeObserver(() => this.checkOverflow());
    this.resizeObserver.observe(wrapper);
    this.checkOverflow();
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
  }

  toggleExpanded(): void {
    this.expanded.update((v) => !v);
  }

  private checkOverflow(): void {
    const wrapper = this.contentWrapper();
    if (wrapper) {
      const el = wrapper.nativeElement;
      this.isOverflowing.set(el.scrollHeight > MAX_HEIGHT_PX);
    }
  }
}

