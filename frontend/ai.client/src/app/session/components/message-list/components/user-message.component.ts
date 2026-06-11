import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
  ElementRef,
  viewChild,
  AfterViewInit,
  inject,
} from '@angular/core';
import { ContentBlock, Message, FileAttachmentData } from '../../../services/models/message.model';
import { FileAttachmentBadgeComponent, ImageAttachmentGroupComponent } from './file-attachment';
import { LocalSettingsService } from '../../../../services/local-settings.service';

function isImageMimeType(mimeType: string): boolean {
  return mimeType.startsWith('image/');
}

const MAX_HEIGHT_PX = 200;

@Component({
  selector: 'app-user-message',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FileAttachmentBadgeComponent, ImageAttachmentGroupComponent],
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

        <!-- Text content (message bubble) -->
        @if (hasTextContent()) {
          <div
            class="max-w-[80%] rounded-2xl bg-primary-500 px-4 py-3 text-base/6 text-white/90"
          >
            <div class="relative">
              <div
                #contentWrapper
                class="overflow-hidden transition-[max-height] duration-300 ease-in-out"
                [style.max-height]="expanded() ? 'none' : maxHeightPx + 'px'"
              >
                @if (displayText()) {
                  <p class="whitespace-pre-wrap">{{ displayText() }}</p>
                } @else {
                  @for (block of message().content; track $index) {
                    @if (block.type === 'text' && block.text) {
                      <p class="whitespace-pre-wrap">{{ block.text }}</p>
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
export class UserMessageComponent implements AfterViewInit {
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

    return new Date(sentMs).toLocaleString(undefined, {
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

  ngAfterViewInit(): void {
    this.checkOverflow();
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

