import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroLightBulb, heroChevronRight, heroLockClosed, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { ContentBlock, ReasoningContentData } from '../../../../services/models/message.model';

@Component({
  selector: 'app-reasoning-content',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroLightBulb, heroChevronRight, heroLockClosed, heroExclamationTriangle })],
  template: `
    <!-- Reasoning content wrapper with subtle gradient border -->
    <div class="rounded-md bg-linear-to-br from-gray-300/40 to-gray-400/40 dark:from-gray-600/30 dark:to-gray-500/30 p-px">
      <div class="rounded-[calc(0.375rem-1px)] bg-gray-100/60 dark:bg-gray-800/40 px-2 py-1.5">
        <!-- Header with toggle -->
        <button
          type="button"
          class="flex w-full items-center gap-1.5 border-none bg-transparent p-0 font-inherit cursor-pointer transition-opacity duration-150 hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 focus-visible:rounded-xs"
          [attr.aria-expanded]="isExpanded()"
          aria-controls="reasoning-content"
          (click)="toggleExpanded()"
        >
          <!-- Brain/thinking icon -->
          <ng-icon
            name="heroLightBulb"
            class="size-4 shrink-0 text-gray-500 dark:text-gray-400"
            aria-hidden="true"
          />

          <!-- Expand/collapse chevron -->
          <ng-icon
            name="heroChevronRight"
            class="size-3 shrink-0 text-gray-400 dark:text-gray-500 transition-transform duration-200"
            [class.rotate-90]="isExpanded()"
            aria-hidden="true"
          />

          <!-- Label and inline preview -->
          <span class="flex flex-1 items-center gap-2 min-w-0">
            <span class="shrink-0 text-sm/5 font-medium text-gray-600 dark:text-gray-400">Thinking</span>
            @if (!isExpanded() && hasReasoningText()) {
              <span class="truncate text-xs text-gray-500 dark:text-gray-500 italic min-w-0">{{ previewText() }}</span>
            }
          </span>

          <!-- Redacted indicator -->
          @if (hasRedactedContent()) {
            <span class="inline-flex shrink-0 items-center gap-1 rounded-full bg-amber-500/15 dark:bg-amber-500/20 px-1.5 py-0.5 text-[0.625rem] font-medium text-amber-700 dark:text-amber-300">
              <ng-icon name="heroLockClosed" class="size-3" aria-hidden="true" />
              Redacted
            </span>
          }
        </button>

        <!-- Collapsible content -->
        <div
          id="reasoning-content"
          class="grid transition-[grid-template-rows] duration-250 ease-out"
          [class.grid-rows-[0fr]]="!isExpanded()"
          [class.grid-rows-[1fr]]="isExpanded()"
        >
          <div class="overflow-hidden">
            <div class="pt-2">
              @if (hasReasoningText()) {
                <div class="max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded-xs border border-gray-300/50 dark:border-gray-600/40 bg-gray-200/30 dark:bg-gray-700/30 p-2 font-mono text-xs/relaxed text-gray-600 dark:text-gray-400">
                  {{ reasoningText() }}
                </div>
              }

              @if (hasRedactedContent() && !hasReasoningText()) {
                <div class="flex items-center gap-1.5 rounded-xs border border-amber-300/50 dark:border-amber-700/30 bg-amber-100/50 dark:bg-amber-900/20 p-2 text-xs text-amber-800 dark:text-amber-200">
                  <ng-icon
                    name="heroExclamationTriangle"
                    class="size-4 text-amber-500"
                    aria-hidden="true"
                  />
                  <span>Some reasoning content was redacted for safety purposes.</span>
                </div>
              }
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
  styles: `
    :host {
      display: block;
    }
  `
})
export class ReasoningContentComponent {
  /** The content block containing reasoning data */
  contentBlock = input.required<ContentBlock>();

  /** Whether the reasoning content is expanded */
  isExpanded = signal(false);

  /** Extract reasoning content data from the content block */
  reasoningData = computed(() => {
    const block = this.contentBlock();
    return block.reasoningContent as ReasoningContentData | null;
  });

  /** Get the reasoning text */
  reasoningText = computed(() => {
    const data = this.reasoningData();
    return data?.reasoningText?.text || '';
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
