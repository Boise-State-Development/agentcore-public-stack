import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  PLATFORM_ID,
  signal,
} from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSquare2Stack, heroCheck } from '@ng-icons/heroicons/outline';
import { MarkdownService } from 'ngx-markdown';
import { Message, isTextContentBlock } from '../../../services/models/message.model';
import { TooltipDirective } from '../../../../components/tooltip';

@Component({
  selector: 'app-message-actions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [provideIcons({ heroSquare2Stack, heroCheck })],
  template: `
    <div class="flex items-center gap-1">
      <button
        type="button"
        class="inline-flex items-center justify-center rounded-md p-1.5 text-gray-500 transition-colors hover:bg-gray-200 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
        [appTooltip]="copied() ? 'Copied' : 'Copy'"
        appTooltipPosition="top"
        [attr.aria-label]="copied() ? 'Copied to clipboard' : 'Copy message'"
        [disabled]="!hasCopyableText()"
        (click)="copy()"
      >
        @if (copied()) {
          <ng-icon name="heroCheck" class="size-4" aria-hidden="true" />
        } @else {
          <ng-icon name="heroSquare2Stack" class="size-4" aria-hidden="true" />
        }
      </button>
    </div>
  `,
  styles: `
    @import 'tailwindcss';
    @custom-variant dark (&:where(.dark, .dark *));

    :host {
      display: contents;
    }

    button[disabled] {
      opacity: 0.5;
      cursor: not-allowed;
    }
  `,
})
export class MessageActionsComponent {
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);
  private markdown = inject(MarkdownService);

  message = input.required<Message>();

  protected copied = signal(false);
  private resetTimeout: ReturnType<typeof setTimeout> | null = null;

  protected copyableText = computed(() =>
    this.message()
      .content.filter(isTextContentBlock)
      .map((block) => block.text)
      .join('\n\n')
      .trim(),
  );

  protected hasCopyableText = computed(() => this.copyableText().length > 0);

  async copy(): Promise<void> {
    if (!this.isBrowser || !this.hasCopyableText()) return;

    const markdown = this.copyableText();

    try {
      await this.writeRichClipboard(markdown);
      this.copied.set(true);

      if (this.resetTimeout) {
        clearTimeout(this.resetTimeout);
      }
      this.resetTimeout = setTimeout(() => {
        this.copied.set(false);
        this.resetTimeout = null;
      }, 2000);
    } catch {
      // Clipboard API may be unavailable (insecure context, permissions). No-op.
    }
  }

  /**
   * Write both rendered HTML and raw markdown to the clipboard so rich-text
   * targets (Google Docs, Word, Gmail) paste formatted content while plain-
   * text targets (terminals, code editors) still get the markdown source.
   * Falls back to plain-text only when ClipboardItem isn't available.
   */
  private async writeRichClipboard(markdown: string): Promise<void> {
    const html = await Promise.resolve(this.markdown.parse(markdown));
    const fullHtml = `<meta charset="utf-8">${html}`;

    if (typeof ClipboardItem !== 'undefined' && navigator.clipboard.write) {
      const item = new ClipboardItem({
        'text/html': new Blob([fullHtml], { type: 'text/html' }),
        'text/plain': new Blob([markdown], { type: 'text/plain' }),
      });
      await navigator.clipboard.write([item]);
      return;
    }

    await navigator.clipboard.writeText(markdown);
  }
}
