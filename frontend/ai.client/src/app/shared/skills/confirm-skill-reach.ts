import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';

/**
 * Ask before a skill edit reaches other people's agents or projects.
 *
 * `reach` is the server's summary (the same paragraph the `my_skill_*` approval
 * card shows), rendered as text by the dialog. Resolves true only on Save anyway.
 */
export async function confirmSkillReach(dialog: Dialog, reach: string): Promise<boolean> {
  const ref = dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
    data: {
      title: 'This change reaches other people',
      message: reach,
      confirmText: 'Save anyway',
      cancelText: 'Keep editing',
    },
  });
  return (await firstValueFrom(ref.closed)) === true;
}
