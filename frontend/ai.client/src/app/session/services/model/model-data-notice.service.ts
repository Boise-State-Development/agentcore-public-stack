import { Injectable, inject } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog/confirmation-dialog.component';
import {
  ManagedModel,
  keepsConversationsForReview,
} from '../../../admin/manage-models/models/managed-model.model';
import { UserSettingsService } from '../../../services/user-settings.service';

/**
 * The data-handling notice a user confirms before choosing a model whose
 * provider keeps conversations for review (`apis/shared/models/data_handling.py`).
 *
 * Asked once per model and wording: a confirmation is stored on the user's
 * settings with the notice's digest, so a reworded notice is asked again.
 * Every place a user chooses a model calls {@link confirmBeforeChoosing} first:
 * the chat model picker and the default-model setting.
 *
 * The notice protects the user's choice, not the turn: nothing here sits on
 * the turn path, and RBAC grants remain what decides who can run the model.
 */
@Injectable({ providedIn: 'root' })
export class ModelDataNoticeService {
  private readonly dialog = inject(Dialog);
  private readonly settings = inject(UserSettingsService);

  /** Whether the user may go ahead with `model`: true for a standard model. */
  async confirmBeforeChoosing(model: ManagedModel): Promise<boolean> {
    if (!keepsConversationsForReview(model)) return true;
    if (await this.alreadyConfirmed(model)) return true;

    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: `Before you use ${model.modelName}`,
        message:
          model.dataHandlingNoticeText ??
          'Conversations with this model are kept by its provider for review.',
        confirmText: `Use ${model.modelName}`,
        cancelText: 'Keep my current model',
      } satisfies ConfirmationDialogData,
    });
    if ((await firstValueFrom(ref.closed)) !== true) return false;

    try {
      await this.settings.acknowledgeModelNotice(model.modelId);
    } catch {
      // The user did confirm; failing to store it only means asking again.
    }
    return true;
  }

  private async alreadyConfirmed(model: ManagedModel): Promise<boolean> {
    try {
      const current = await this.settings.getSettings();
      const entry = current.modelNoticeAcknowledgements?.[model.modelId];
      return !!entry && entry.noticeDigest === (model.dataHandlingNoticeDigest ?? null);
    } catch {
      return false;
    }
  }
}
