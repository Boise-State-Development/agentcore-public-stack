import { ChangeDetectionStrategy, Component, OnDestroy, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSquare2Stack, heroCheck } from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../../components/tooltip';

/**
 * Custom button rendered by ngx-markdown for the copy-to-clipboard toolbar
 * that wraps each `<pre>` code block. ngx-markdown attaches ClipboardJS to
 * the rendered button's root node — this component only owns the visual
 * "Copied" feedback state.
 */
@Component({
  selector: 'app-code-block-clipboard-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [provideIcons({ heroSquare2Stack, heroCheck })],
  template: `
    <button
      type="button"
      class="inline-flex items-center justify-center rounded-md bg-gray-100/80 p-1.5 text-gray-500 backdrop-blur-sm transition-colors hover:bg-gray-200 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-gray-800/80 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
      [appTooltip]="copied() ? 'Copied' : 'Copy code'"
      appTooltipPosition="top"
      [attr.aria-label]="copied() ? 'Copied to clipboard' : 'Copy code'"
      (click)="onClick()"
    >
      @if (copied()) {
        <ng-icon name="heroCheck" class="size-4" aria-hidden="true" />
      } @else {
        <ng-icon name="heroSquare2Stack" class="size-4" aria-hidden="true" />
      }
    </button>
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class CodeBlockClipboardButtonComponent implements OnDestroy {
  protected copied = signal(false);
  private resetTimeout: ReturnType<typeof setTimeout> | null = null;

  protected onClick(): void {
    this.copied.set(true);
    if (this.resetTimeout) {
      clearTimeout(this.resetTimeout);
    }
    this.resetTimeout = setTimeout(() => {
      this.copied.set(false);
      this.resetTimeout = null;
    }, 2000);
  }

  ngOnDestroy(): void {
    if (this.resetTimeout) {
      clearTimeout(this.resetTimeout);
    }
  }
}
