import { DestroyRef, Directive, HostAttributeToken, inject } from '@angular/core';
import { injectHostDialog, parseDialogRole, setHostDialogRole } from './host-dialog';

let nextId = 0;

/**
 * Names the CDK dialog this element sits in after the element's text — the job Angular
 * Material's `mat-dialog-title` does. Put it on the dialog's heading:
 *
 * ```html
 * <h2 appDialogTitle>Delete this model?</h2>
 * <h2 appDialogTitle dialogRole="alertdialog">Delete this model?</h2>   <!-- a confirmation -->
 * ```
 *
 * CDK's `.cdk-dialog-container` is the `role="dialog"` element and has no name unless the
 * opener passes `ariaLabel`/`ariaLabelledBy` (axe `aria-dialog-name`, critical). This
 * gives the heading an id and adds it to the container's `aria-labelledby`, and removes
 * it when the heading goes away. An opener's own `ariaLabel` still wins. Don't draw a
 * second `role="dialog"` panel inside: assistive tech would meet a dialog in a dialog.
 *
 * `dialogRole` is read once, as a static attribute, because the container's role must be
 * set before it first renders.
 *
 * Keep the heading out of `@if`/`@for`, so it exists when the dialog opens. Outside a CDK
 * dialog (a spec rendering the component directly) it only sets the id; `hosted` says which.
 */
@Directive({
  selector: '[appDialogTitle]',
  exportAs: 'appDialogTitle',
  host: { '[id]': 'id' },
})
export class DialogTitleDirective {
  readonly id = `dialog-title-${++nextId}`;

  private readonly dialog = injectHostDialog();
  /** Rendered inside a CDK dialog's container, which this title names. */
  readonly hosted = !!this.dialog;

  constructor() {
    const dialog = this.dialog;
    if (!dialog) return;
    const role = parseDialogRole(inject(new HostAttributeToken('dialogRole'), { optional: true }));
    if (role) setHostDialogRole(dialog, role);
    dialog.container._addAriaLabelledBy?.(this.id);
    inject(DestroyRef).onDestroy(() => dialog.container._removeAriaLabelledBy?.(this.id));
  }
}
