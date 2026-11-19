import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';

/** The dialog's words; an edit uses the defaults, a delete its own. */
export interface SkillReachLabels {
  title: string;
  confirmText: string;
  cancelText: string;
  destructive?: boolean;
}

const EDIT_LABELS: SkillReachLabels = {
  title: 'This change reaches other people',
  confirmText: 'Save anyway',
  cancelText: 'Keep editing',
};

/** For `DELETE /skills/mine/{id}`, whose 409 names who loses the skill. */
export const DELETE_REACH_LABELS: SkillReachLabels = {
  title: 'Other people use this skill',
  confirmText: 'Delete anyway',
  cancelText: 'Keep it',
  destructive: true,
};

/**
 * Ask before a skill edit (or delete) reaches other people's agents or projects.
 *
 * `reach` is the server's summary (for an edit, the same paragraph the `my_skill_*`
 * approval card shows), rendered as text by the dialog. Resolves true only on confirm.
 */
export async function confirmSkillReach(
  dialog: Dialog,
  reach: string,
  labels: SkillReachLabels = EDIT_LABELS,
): Promise<boolean> {
  const ref = dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
    data: {
      title: labels.title,
      message: reach,
      confirmText: labels.confirmText,
      cancelText: labels.cancelText,
      destructive: labels.destructive,
    },
  });
  return (await firstValueFrom(ref.closed)) === true;
}
