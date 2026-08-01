import { Directive, output } from '@angular/core';

/**
 * Dismiss a dialog when the user clicks outside its panel.
 *
 * Goes on the **full-screen centring container**, not the backdrop — which is the whole
 * reason this exists. Every dialog in this codebase is built as two siblings:
 *
 * ```
 * <div class="dialog-backdrop fixed inset-0 …"></div>
 * <div class="fixed inset-0 z-10 flex … ">   <-- put the directive here
 *   <div class="dialog-panel …">…</div>
 * </div>
 * ```
 *
 * The container is itself `inset-0`, so it covers the backdrop completely and every click
 * aimed at the backdrop lands on the container instead. A `(click)` handler on the
 * backdrop reads perfectly and can never fire — which is exactly what shipped, in every
 * dialog, for as long as they have existed.
 *
 * ⚠️ **The mousedown guard is not optional.** Selecting text inside the panel and
 * releasing the mouse outside it delivers a click to this container, because the browser
 * dispatches to the nearest common ancestor of the press and the release. Without the
 * guard, dragging to select a word in a form field throws the user's unsaved work away.
 * So a dismissal requires the press *and* the release to land on the container itself.
 */
@Directive({
  selector: '[appDialogDismiss]',
  host: {
    '(mousedown)': 'onPressStart($event)',
    '(click)': 'onPressEnd($event)',
  },
})
export class DialogDismissDirective {
  /** Emitted only for a click that began and ended on the host. Wire to the cancel path. */
  readonly dismissed = output<void>();

  /** True only while a press that *started* on the host is still in flight. */
  private pressStartedOnHost = false;

  onPressStart(event: MouseEvent): void {
    this.pressStartedOnHost = event.target === event.currentTarget;
  }

  onPressEnd(event: MouseEvent): void {
    const startedAndEndedOnHost =
      this.pressStartedOnHost && event.target === event.currentTarget;
    // Cleared unconditionally: a press that began in the panel must not leave the next
    // bare click armed.
    this.pressStartedOnHost = false;
    if (startedAndEndedOnHost) this.dismissed.emit();
  }
}
