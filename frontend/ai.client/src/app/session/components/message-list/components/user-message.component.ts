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
import { FileAttachmentBadgeComponent } from './file-attachment';
import { LocalSettingsService } from '../../../../services/local-settings.service';

const MAX_HEIGHT_PX = 200;

@Component({
  selector: 'app-user-message',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FileAttachmentBadgeComponent],
  template: `
    @if (hasTextContent() || hasFileAttachments()) {
      <div class="flex w-full flex-col items-end gap-2">
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

        <!-- File attachments (below message bubble) -->
        @if (hasFileAttachments()) {
          <div class="flex max-w-[80%] flex-wrap justify-end gap-2">
            @for (attachment of fileAttachments(); track attachment.uploadId) {
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

