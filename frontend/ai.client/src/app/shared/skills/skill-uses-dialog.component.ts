import { Dialog, DialogRef, DIALOG_DATA } from '@angular/cdk/dialog';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { SkillUsesComponent } from './skill-uses.component';
import { SkillUses, SkillUsesApi } from './skill-uses.model';

export interface SkillUsesDialogData {
  api: SkillUsesApi;
  skillId: string;
  uses: SkillUses;
}

/** Closed either way; nothing to return. `undefined` is also Escape and the close button. */
export type SkillUsesDialogResult = undefined;

/**
 * The offer that follows a change to a skill (docs/specs/skill-version-history.md
 * D9): the places pinned to an older version, ticked, with Update selected and
 * Not now. Opened by `offerPinUpdates` only when something is behind, so a save
 * that reached everything shows nothing.
 */
@Component({
  selector: 'app-skill-uses-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, SkillUsesComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Some places still use an earlier version"
      description="Pinned agents and projects keep the version they pinned until someone updates them. Choose which to bring up to date."
      (closed)="close()"
    >
      <app-skill-uses
        mode="offer"
        [api]="data.api"
        [skillId]="data.skillId"
        [initial]="data.uses"
        (done)="close()"
      />
    </app-dialog-shell>
  `,
})
export class SkillUsesDialogComponent {
  protected readonly data = inject<SkillUsesDialogData>(DIALOG_DATA);
  private readonly dialogRef = inject<DialogRef<SkillUsesDialogResult>>(DialogRef);

  protected close(): void {
    this.dialogRef.close(undefined);
  }
}

/**
 * After a change to an owned skill: if pinned places are behind and yours to
 * update, ask whether to update them, and resolve once the author has answered.
 * Reads where the skill is used first, so the dialog never opens empty; a read
 * that fails stays quiet, because the change itself already succeeded.
 */
export async function offerPinUpdates(
  dialog: Dialog,
  api: SkillUsesApi,
  skillId: string,
): Promise<void> {
  let uses: SkillUses;
  try {
    uses = await api.getUses(skillId);
  } catch {
    return;
  }
  if (uses.behind.length === 0) return;
  const ref = dialog.open<SkillUsesDialogResult, SkillUsesDialogData>(SkillUsesDialogComponent, {
    data: { api, skillId, uses },
  });
  await firstValueFrom(ref.closed);
}
