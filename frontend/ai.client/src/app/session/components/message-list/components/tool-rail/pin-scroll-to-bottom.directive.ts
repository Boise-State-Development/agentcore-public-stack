import { Directive, ElementRef, effect, inject, input } from '@angular/core';

/**
 * Keeps the host element scrolled to the bottom whenever the bound value
 * changes. Used for live-streaming output (e.g. artifact generation) so the
 * latest content stays visible inside a fixed-height scroll box.
 */
@Directive({
  selector: '[appPinScrollToBottom]',
})
export class PinScrollToBottomDirective {
  /** Bind the streamed value; the host re-pins to the bottom on each change. */
  readonly appPinScrollToBottom = input<string>('');

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  constructor() {
    effect(() => {
      // Read the streamed value so the effect re-runs for every chunk.
      this.appPinScrollToBottom();
      const el = this.host.nativeElement;
      el.scrollTop = el.scrollHeight;
    });
  }
}
