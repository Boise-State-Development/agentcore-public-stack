import {
  Component,
  input,
  signal,
  effect,
  OnDestroy,
  ChangeDetectionStrategy,
  PLATFORM_ID,
  inject,
} from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { MarkdownComponent } from 'ngx-markdown';
import { CodeBlockClipboardButtonComponent } from './code-block-clipboard-button.component';

/**
 * StreamingTextComponent provides smooth character-by-character typing animation
 * for streaming AI responses.
 *
 * Uses requestAnimationFrame with dynamic character batching for 60fps smooth animation.
 * When streaming completes, immediately shows full text to ensure no content is lost.
 *
 * Inspired by: https://github.com/aws-samples/sample-strands-agent-with-agentcore
 */
@Component({
  selector: 'app-streaming-text',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownComponent],
  template: `
    <markdown
      class="min-w-0 max-w-full overflow-hidden"
      [clipboard]="!isStreaming()"
      [clipboardButtonComponent]="ClipboardButton"
      mermaid
      katex
      [data]="displayedText()"
    ></markdown>
  `,
  styles: `
    :host {
      display: block;
    }
  `,
})
export class StreamingTextComponent implements OnDestroy {
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);

  protected readonly ClipboardButton = CodeBlockClipboardButtonComponent;

  /** The full text content to display */
  text = input.required<string>();

  /** Whether this text block is currently being streamed */
  isStreaming = input<boolean>(false);

  /** The text currently displayed (animated subset of full text) */
  displayedText = signal('');

  // Animation state
  private animationFrameId: number | null = null;
  private displayedLength = 0;
  private lastAnimationTime = 0;

  constructor() {
    effect(() => {
      const currentText = this.text();
      const streaming = this.isStreaming();

      if (!this.isBrowser) {
        // SSR: show full text immediately
        this.displayedText.set(currentText);
        return;
      }

      if (streaming && currentText.length > this.displayedLength) {
        // New text arrived while streaming - animate it
        this.startAnimation();
      } else if (!streaming) {
        // Streaming ended - show full text immediately
        this.cancelAnimation();
        this.displayedText.set(currentText);
        this.displayedLength = currentText.length;
      }
    });
  }

  ngOnDestroy(): void {
    this.cancelAnimation();
  }

  private startAnimation(): void {
    if (this.animationFrameId !== null) {
      // Animation already running
      return;
    }
    this.lastAnimationTime = performance.now();
    this.animate();
  }

  /**
   * Animation loop using requestAnimationFrame.
   *
   * Uses dynamic character pacing:
   * - When there's lots of text to catch up, adds characters faster
   * - Bounds: 2-8ms per character
   * - Max 5 characters per frame for natural feel
   * - Targets completing new text within ~50ms (matches typical SSE buffer flush)
   */
  private animate = (): void => {
    const targetLength = this.text().length;

    if (this.displayedLength >= targetLength) {
      // Caught up to current text
      this.animationFrameId = null;
      return;
    }

    const now = performance.now();
    const elapsed = now - this.lastAnimationTime;
    const charsRemaining = targetLength - this.displayedLength;

    // Dynamic pacing: faster when more text to catch up
    // Target: animate all new text within ~50ms
    const msPerChar = Math.max(2, Math.min(8, 50 / Math.max(charsRemaining, 1)));

    if (elapsed >= msPerChar) {
      const charsToAdd = Math.min(
        Math.ceil(elapsed / msPerChar),
        5, // Max 5 chars per frame for natural feel
        charsRemaining
      );

      this.displayedLength += charsToAdd;
      this.lastAnimationTime = now;
      this.displayedText.set(this.text().slice(0, this.displayedLength));
    }

    this.animationFrameId = requestAnimationFrame(this.animate);
  };

  private cancelAnimation(): void {
    if (this.animationFrameId !== null) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }
  }
}
