import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog/confirmation-dialog.component';

/**
 * Ask an admin to confirm putting a provider_review model in users' reach.
 *
 * The backend refuses (409) that write without `acknowledgeDataHandling: true`
 * and audits it with one, so this is the only path the UI takes to it: the list
 * toggle and the edit form both call it. The dialog quotes the notice users
 * will see, so the admin approves the exact wording, not a summary of it.
 */
export async function confirmProviderReviewEnable(
  dialog: Dialog,
  modelName: string,
  noticeText: string | null | undefined,
): Promise<boolean> {
  const notice = noticeText?.trim();
  const ref = dialog.open<boolean>(ConfirmationDialogComponent, {
    data: {
      title: `Make ${modelName} available?`,
      message:
        'Its provider keeps conversations for review, so it works only once the AWS account has ' +
        'opted in to the aws_review data-retention mode. Users confirm this notice before their ' +
        'first message on it' +
        (notice ? `: “${notice}”` : '.') +
        ' Your confirmation is recorded in the audit log.',
      confirmText: 'Make available',
      cancelText: 'Cancel',
    } satisfies ConfirmationDialogData,
  });
  return (await firstValueFrom(ref.closed)) === true;
}
