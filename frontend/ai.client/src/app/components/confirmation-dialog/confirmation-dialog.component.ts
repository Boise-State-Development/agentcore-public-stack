import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../dialog/dialog-shell.component';

/**
 * Data passed to the confirmation dialog.
 */
export interface ConfirmationDialogData {
  /** Title displayed at the top of the dialog */
  title: string;
  /** Description/message explaining what the action will do */
  message: string;
  /** Text for the confirm button (default: "Confirm") */
  confirmText?: string;
  /** Text for the cancel button (default: "Cancel") */
  cancelText?: string;
  /** Whether this is a destructive action (shows red styling) */
  destructive?: boolean;
}

/**
 * A reusable confirmation dialog component using Angular CDK Dialog.
 *
 * Features:
 * - Accessible: an `alertdialog` named by its title, focus trapped and starting on Cancel
 * - Responsive: Works on mobile and desktop
 * - Dark mode support
 * - Configurable: Title, message, button text, destructive styling
 *
 * @example
 * ```typescript
 * import { Dialog } from '@angular/cdk/dialog';
 * import { ConfirmationDialogComponent, ConfirmationDialogData } from './confirmation-dialog.component';
 *
 * // In your component
 * private dialog = inject(Dialog);
 *
 * async confirmDelete(): Promise<boolean> {
 *   const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
 *     data: {
 *       title: 'Delete Item',
 *       message: 'Are you sure you want to delete this item? This action cannot be undone.',
 *       confirmText: 'Delete',
 *       cancelText: 'Cancel',
 *       destructive: true
 *     } as ConfirmationDialogData
 *   });
 *
 *   const result = await firstValueFrom(dialogRef.closed);
 *   return result === true;
 * }
 * ```
 */
@Component({
  selector: 'app-confirmation-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <!-- An alertdialog, so assistive tech announces it as an interruption. Focus starts on
         Cancel: the least destructive choice, so a stray Enter never confirms. -->
    <app-dialog-shell [title]="data.title" [description]="data.message" dialogRole="alertdialog" (closed)="onCancel()">
      @if (data.destructive) {
        <div
          dialogIcon
          class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-state-danger-100 dark:bg-state-danger-500/10"
        >
          <ng-icon name="heroExclamationTriangle" class="size-5 text-state-danger-600 dark:text-state-danger-400" aria-hidden="true" />
        </div>
      }

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          cdkFocusInitial
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          {{ data.cancelText || 'Cancel' }}
        </button>
        <button type="button" (click)="onConfirm()" [class]="confirmButtonClass">
          {{ data.confirmText || 'Confirm' }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class ConfirmationDialogComponent {
  protected readonly dialogRef = inject(DialogRef<boolean>);
  protected readonly data = inject<ConfirmationDialogData>(DIALOG_DATA);

  /**
   * Returns the appropriate CSS classes for the confirm button based on whether
   * this is a destructive action or not.
   */
  protected get confirmButtonClass(): string {
    const baseClasses =
      'rounded-2xl px-4 py-2 text-sm/6 font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2';

    if (this.data.destructive) {
      return `${baseClasses} bg-state-danger-600 hover:bg-state-danger-700 focus-visible:outline-state-danger-500 dark:bg-state-danger-500 dark:hover:bg-state-danger-600`;
    }

    return `${baseClasses} bg-primary-accessible hover:brightness-95 focus-visible:outline-primary-500`;
  }

  /**
   * Called when the user confirms the action.
   * Closes the dialog with `true` result.
   */
  protected onConfirm(): void {
    this.dialogRef.close(true);
  }

  /**
   * Called when the user cancels or dismisses the dialog.
   * Closes the dialog with `false` result.
   */
  protected onCancel(): void {
    this.dialogRef.close(false);
  }
}

