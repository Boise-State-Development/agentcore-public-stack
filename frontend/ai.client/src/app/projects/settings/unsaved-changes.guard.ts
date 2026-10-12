import { inject } from '@angular/core';
import { CanDeactivateFn } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';

/** A page that holds a draft until its Save. */
export interface HasUnsavedChanges {
  hasUnsavedChanges(): boolean;
}

/**
 * Ask before leaving a settings page with an unsaved draft (instructions, tools,
 * skills). In a dialog, leaving was an explicit Cancel; on a page it is any link,
 * so the question has to be asked here. A reload or a closed tab is the page's own
 * `beforeunload`, which the browser words itself.
 */
export const unsavedChangesGuard: CanDeactivateFn<HasUnsavedChanges> = async component => {
  if (!component.hasUnsavedChanges()) return true;
  const ref = inject(Dialog).open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
    data: {
      title: 'Discard your changes?',
      message: 'You haven’t saved them. Leaving this page loses them.',
      confirmText: 'Discard changes',
      cancelText: 'Keep editing',
      destructive: true,
    },
  });
  return (await firstValueFrom(ref.closed)) === true;
};
