import { DestroyRef, Directive, effect, inject } from '@angular/core';
import { injectHostDialog } from './host-dialog';

let nextId = 0;

/**
 * Describes the CDK dialog this element sits in — the companion to `appDialogTitle`:
 *
 * ```html
 * <h2 appDialogTitle>Decline this submission</h2>
 * <p appDialogDescription>The author sees your reason on their card.</p>
 * ```
 *
 * The container's own `aria-describedby` comes only from the opener's `ariaDescribedBy`.
 * When that is empty this element's id fills it, and an opener's value is left alone. It
 * is written straight onto the container element, after the container's own binding has
 * rendered (so that binding's first, empty, write cannot erase it), and cleared when the
 * element goes away — so it can sit inside `@if`.
 */
@Directive({
  selector: '[appDialogDescription]',
  exportAs: 'appDialogDescription',
  host: { '[id]': 'id' },
})
export class DialogDescriptionDirective {
  readonly id = `dialog-description-${++nextId}`;

  constructor() {
    const element = injectHostDialog()?.container._elementRef?.nativeElement;
    if (!element) return;
    effect(() => {
      if (!element.getAttribute('aria-describedby')) element.setAttribute('aria-describedby', this.id);
    });
    inject(DestroyRef).onDestroy(() => {
      if (element.getAttribute('aria-describedby') === this.id) element.removeAttribute('aria-describedby');
    });
  }
}
