import { ChangeDetectorRef, ElementRef, inject } from '@angular/core';
import { DialogRef, DialogRole } from '@angular/cdk/dialog';

/**
 * The parts of CDK's dialog container that name it. `_addAriaLabelledBy` is the hook
 * Angular Material's dialog title uses; they're all underscored, so they're reached
 * through this narrow, optional shape and a missing member just leaves the name off.
 */
interface CdkDialogContainerInternals {
  _addAriaLabelledBy?(id: string): void;
  _removeAriaLabelledBy?(id: string): void;
  _elementRef?: ElementRef<HTMLElement>;
  _changeDetectorRef?: ChangeDetectorRef;
}

/** The CDK dialog the injecting component was opened in. */
export interface HostDialog {
  readonly ref: DialogRef<unknown>;
  readonly container: CdkDialogContainerInternals;
}

/**
 * The CDK dialog this injection context renders in, or `null` outside one (a spec or
 * harness rendering the component directly, or a stub `DialogRef` with no container).
 *
 * CDK's `.cdk-dialog-container` is itself the `role="dialog"` element, so it is the one
 * that needs the accessible name (axe `aria-dialog-name`) — not a panel drawn inside it.
 */
export function injectHostDialog(): HostDialog | null {
  const ref = inject(DialogRef, { optional: true });
  const container = ref?.containerInstance as unknown as CdkDialogContainerInternals | undefined;
  return ref && container ? { ref, container } : null;
}

/**
 * Give the container a role other than the opener's (CDK defaults to `dialog`), so a
 * confirmation is an `alertdialog` however it was opened. The container binds its role
 * from the same config object the ref holds; call this before its first change detection.
 */
export function setHostDialogRole(dialog: HostDialog, role: DialogRole): void {
  dialog.ref.config.role = role;
  dialog.container._changeDetectorRef?.markForCheck();
}

/** Narrow a static `dialogRole` attribute to a role CDK accepts. */
export function parseDialogRole(value: string | null): DialogRole | null {
  return value === 'dialog' || value === 'alertdialog' ? value : null;
}
