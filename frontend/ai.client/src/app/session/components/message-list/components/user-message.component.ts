import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
  ElementRef,
  viewChild,
  AfterViewInit,
} from '@angular/core';
import { ContentBlock, Message } from '../../../services/models/message.model';

const MAX_HEIGHT_PX = 200;

@Component({
  selector: 'app-user-message',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (hasTextContent()) {
      <div class="flex w-full justify-end">
        <div
          class="max-w-[80%] rounded-2xl bg-primary-500 px-4 py-3 text-base/6 text-white/90"
        >
          <div class="relative">
            <div
              #contentWrapper
              class="overflow-hidden transition-[max-height] duration-300 ease-in-out"
              [style.max-height]="expanded() ? 'none' : maxHeightPx + 'px'"
            >
              @for (block of message().content; track $index) {
                @if (block.type === 'text' && block.text) {
                  <p class="whitespace-pre-wrap">{{ block.text }}</p>
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

  readonly maxHeightPx = MAX_HEIGHT_PX;

  hasTextContent = computed(() => {
    return this.message().content.some(
      (block: ContentBlock) => block.type === 'text' && block.text
    );
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

