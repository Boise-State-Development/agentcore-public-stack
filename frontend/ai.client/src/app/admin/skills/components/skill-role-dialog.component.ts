import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  OnInit,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { AdminSkillService } from '../services/admin-skill.service';
import { AdminSkill, SkillRoleAssignment } from '../models/admin-skill.model';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AppRole } from '../../roles/models/app-role.model';
import { DialogDescriptionDirective } from '../../../components/dialog/dialog-description.directive';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

/**
 * Data passed to the skill role dialog.
 */
export interface SkillRoleDialogData {
  skill: AdminSkill;
}

/**
 * Result returned when the dialog is closed: the selected role IDs if saved,
 * or undefined if cancelled.
 */
export type SkillRoleDialogResult = string[] | undefined;

@Component({
  selector: 'app-skill-role-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDescriptionDirective, DialogShellComponent, SpinnerComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Manage Role Access" (closed)="onCancel()">
      <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Select which roles can use <span class="font-medium">{{ data.skill.displayName }}</span>.
      </p>

      @if (loading()) {
        <div class="flex items-center justify-center py-8">
          <app-spinner size="lg" label="Loading" />
        </div>
      } @else {
        <div class="space-y-2">
          @for (role of allRoles(); track role.roleId) {
            <label
              class="flex cursor-pointer items-center gap-3 rounded-lg border border-gray-200 p-3 transition-colors hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-700/50"
              [class.border-primary-500]="selectedRoleIds().has(role.roleId)"
              [class.bg-gray-100]="selectedRoleIds().has(role.roleId)"
              [class.dark:border-primary-400]="selectedRoleIds().has(role.roleId)"
              [class.dark:bg-gray-700]="selectedRoleIds().has(role.roleId)"
            >
              <input
                type="checkbox"
                [checked]="selectedRoleIds().has(role.roleId)"
                (change)="toggleRole(role.roleId)"
                class="size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700"
              />
              <div class="min-w-0 flex-1">
                <div class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ role.displayName }}</div>
                <div class="truncate font-mono text-xs/5 text-gray-600 dark:text-gray-300">{{ role.roleId }}</div>
              </div>
              @if (currentAssignments().has(role.roleId)) {
                <span class="shrink-0 text-xs/5 text-gray-400 dark:text-gray-500">
                  {{ getGrantType(role.roleId) }}
                </span>
              }
            </label>
          }
        </div>

        @if (allRoles().length === 0) {
          <p class="py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">
            No roles available. Create roles first.
          </p>
        }

        <p class="mt-4 text-xs/5 text-state-warning-600 dark:text-state-warning-400">
          Changes take effect within 5-10 minutes.
        </p>
      }

      <div dialogFooter class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700">
        <button
          type="button"
          (click)="onCancel()"
          class="rounded-lg px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="save()"
          [disabled]="saving() || loading()"
          class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:brightness-110"
        >
          {{ saving() ? 'Saving…' : 'Save Changes' }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class SkillRoleDialogComponent implements OnInit {
  protected readonly dialogRef = inject(DialogRef<SkillRoleDialogResult>);
  protected readonly data = inject<SkillRoleDialogData>(DIALOG_DATA);

  private adminSkillService = inject(AdminSkillService);
  private appRolesService = inject(AppRolesService);

  loading = signal(true);
  saving = signal(false);
  allRoles = signal<AppRole[]>([]);
  currentAssignments = signal<Map<string, SkillRoleAssignment>>(new Map());
  selectedRoleIds = signal<Set<string>>(new Set());

  async ngOnInit(): Promise<void> {
    this.loading.set(true);
    try {
      const [rolesResponse, assignments] = await Promise.all([
        this.appRolesService.fetchRoles(),
        this.adminSkillService.getSkillRoles(this.data.skill.skillId),
      ]);

      this.allRoles.set(rolesResponse.roles.filter((r) => r.roleId !== 'system_admin'));

      const assignmentMap = new Map<string, SkillRoleAssignment>();
      for (const a of assignments) {
        assignmentMap.set(a.roleId, a);
      }
      this.currentAssignments.set(assignmentMap);

      const directGrants = assignments
        .filter((a) => a.grantType === 'direct')
        .map((a) => a.roleId);
      this.selectedRoleIds.set(new Set(directGrants));
    } catch (error) {
      console.error('Error loading data:', error);
    } finally {
      this.loading.set(false);
    }
  }

  toggleRole(roleId: string): void {
    this.selectedRoleIds.update((set) => {
      const next = new Set(set);
      if (next.has(roleId)) {
        next.delete(roleId);
      } else {
        next.add(roleId);
      }
      return next;
    });
  }

  getGrantType(roleId: string): string {
    const assignment = this.currentAssignments().get(roleId);
    if (!assignment) return '';
    if (assignment.grantType === 'inherited') {
      return `inherited from ${assignment.inheritedFrom}`;
    }
    return 'direct';
  }

  save(): void {
    this.saving.set(true);
    try {
      this.dialogRef.close(Array.from(this.selectedRoleIds()));
    } finally {
      this.saving.set(false);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
