import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroXMark } from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../dialog/dialog-dismiss.directive';

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
 * - Accessible: Proper ARIA attributes, focus trap, keyboard navigation
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
  imports: [DialogDismissDirective, NgIcon],
  providers: [provideIcons({ heroExclamationTriangle, heroXMark })],
  host: {
    'class': 'block',
    '(keydown.escape)': 'onCancel()'
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()">
      <div
        class="dialog-panel relative transform overflow-hidden rounded-lg bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-lg sm:p-6 dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="alertdialog"
        aria-modal="true"
        [attr.aria-labelledby]="'dialog-title'"
        [attr.aria-describedby]="'dialog-description'"
      >
        <!-- Close button (top-right) -->
        <div class="absolute top-0 right-0 hidden pt-4 pr-4 sm:block">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-md bg-white text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-primary-600 dark:bg-gray-800 dark:hover:text-gray-300 dark:focus:outline-white"
            aria-label="Close dialog"
          >
            <span class="sr-only">Close</span>
            <ng-icon name="heroXMark" class="size-6" aria-hidden="true" />
          </button>
        </div>

        <!-- Icon + Content -->
        <div class="sm:flex sm:items-start">
          @if (data.destructive) {
            <div class="mx-auto flex size-12 shrink-0 items-center justify-center rounded-full bg-state-danger-100 sm:mx-0 sm:size-10 dark:bg-state-danger-500/10">
              <ng-icon name="heroExclamationTriangle" class="size-6 text-state-danger-600 dark:text-state-danger-400" aria-hidden="true" />
            </div>
          }
          <div class="mt-3 text-center sm:mt-0 sm:text-left" [class.sm:ml-4]="data.destructive">
            <h3
              id="dialog-title"
              class="text-base/7 font-semibold text-gray-900 dark:text-white"
            >
              {{ data.title }}
            </h3>
            <div class="mt-2">
              <p
                id="dialog-description"
                class="text-sm/6 text-gray-500 dark:text-gray-400"
              >
                {{ data.message }}
              </p>
            </div>
          </div>
        </div>

        <!-- Actions -->
        <div class="mt-5 sm:mt-4 sm:flex sm:flex-row-reverse">
          <button
            type="button"
            (click)="onConfirm()"
            [class]="confirmButtonClass"
          >
            {{ data.confirmText || 'Confirm' }}
          </button>
          <button
            type="button"
            (click)="onCancel()"
            class="mt-3 inline-flex w-full justify-center rounded-md bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 sm:mt-0 sm:w-auto dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
          >
            {{ data.cancelText || 'Cancel' }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    /* Backdrop fade-in animation */
    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from {
        opacity: 0;
      }
      to {
        opacity: 1;
      }
    }

    /* Dialog panel fade-in-up animation */
    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.95);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `
})
export class ConfirmationDialogComponent {
  protected readonly dialogRef = inject(DialogRef<boolean>);
  protected readonly data = inject<ConfirmationDialogData>(DIALOG_DATA);

  /**
   * Returns the appropriate CSS classes for the confirm button based on whether
   * this is a destructive action or not.
   */
  protected get confirmButtonClass(): string {
    const baseClasses = 'inline-flex w-full justify-center rounded-md px-3 py-2 text-sm/6 font-semibold text-white shadow-xs sm:ml-3 sm:w-auto';

    if (this.data.destructive) {
      return `${baseClasses} bg-state-danger-600 hover:bg-state-danger-500 dark:bg-state-danger-500 dark:shadow-none dark:hover:bg-state-danger-400`;
    }

    return `${baseClasses} bg-primary-600 hover:bg-primary-500 dark:bg-primary-500 dark:shadow-none dark:hover:bg-primary-400`;
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

