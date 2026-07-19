import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPlus, heroSparkles, heroPencilSquare, heroTrash, heroDocumentText } from '@ng-icons/heroicons/outline';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { MySkillService } from './services/my-skill.service';

/**
 * "My Skills" — the user-authored half of the skill catalog (Skills v2 PR-3).
 *
 * A skill is a pure knowledge bundle: instructions the agent can pull in on
 * demand, plus supporting files. Skills authored here are private to their
 * author until bound to an Agent (sharing an Agent shares the use of its
 * skills — spec §6 invoke-through).
 */
@Component({
  selector: 'app-my-skills',
  imports: [RouterLink, NgIcon],
  templateUrl: './my-skills.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [
    provideIcons({ heroPlus, heroSparkles, heroPencilSquare, heroTrash, heroDocumentText }),
  ],
})
export class MySkillsPage {
  private skillService = inject(MySkillService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  protected readonly skills = this.skillService.skills$;
  protected readonly loading = this.skillService.loading$;
  protected readonly error = this.skillService.error$;
  protected readonly accessible = this.skillService.accessible$;

  /** Only render "you have no skills yet" once the list has actually resolved. */
  protected readonly isEmpty = computed(
    () => this.accessible() === true && !this.loading() && this.skills().length === 0,
  );

  protected readonly deleting = signal<string | null>(null);

  constructor() {
    void this.skillService.loadSkills();
  }

  protected fileCountLabel(count: number): string {
    if (count === 0) {
      return 'No supporting files';
    }
    return count === 1 ? '1 supporting file' : `${count} supporting files`;
  }

  protected async confirmDelete(skillId: string, displayName: string): Promise<void> {
    const data: ConfirmationDialogData = {
      title: 'Delete this skill?',
      message: `"${displayName}" and its supporting files will be permanently deleted. Agents that use it will stop seeing it.`,
      confirmText: 'Delete',
      cancelText: 'Cancel',
      destructive: true,
    };

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, { data });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (!confirmed) {
      return;
    }

    this.deleting.set(skillId);
    try {
      await this.skillService.deleteSkill(skillId);
    } catch {
      // The service surfaces the message on `error$`; the banner renders it.
    } finally {
      this.deleting.set(null);
    }
  }

  protected openSkill(skillId: string): void {
    void this.router.navigate(['/my-skills', skillId, 'edit']);
  }
}
