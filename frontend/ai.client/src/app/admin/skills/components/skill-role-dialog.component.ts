import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  OnInit,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroUserGroup } from '@ng-icons/heroicons/outline';
import { AdminSkillService } from '../services/admin-skill.service';
import { AdminSkill, SkillRoleAssignment } from '../models/admin-skill.model';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AppRole } from '../../roles/models/app-role.model';

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
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark, heroUserGroup })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="skill-role-title"
        aria-describedby="skill-role-description"
      >
        <!-- Header -->
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="skill-role-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Manage Role Access
            </h2>
            <p id="skill-role-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Select which roles can use <span class="font-medium">{{ data.skill.displayName }}</span>.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <!-- Content -->
        <div class="max-h-72 overflow-y-auto px-6 py-4">
          @if (loading()) {
            <div class="flex items-center justify-center py-8">
              <div class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"></div>
            </div>
          } @else {
            <div class="space-y-2">
              @for (role of allRoles(); track role.roleId) {
                <label
                  class="flex cursor-pointer items-center gap-3 rounded-2xl border border-gray-200 p-3 transition-colors hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-700/50"
                  [class.border-blue-500]="selectedRoleIds().has(role.roleId)"
                  [class.bg-blue-50]="selectedRoleIds().has(role.roleId)"
                  [class.dark:border-blue-400]="selectedRoleIds().has(role.roleId)"
                  [class.dark:bg-blue-900/20]="selectedRoleIds().has(role.roleId)"
                >
                  <input
                    type="checkbox"
                    [checked]="selectedRoleIds().has(role.roleId)"
                    (change)="toggleRole(role.roleId)"
                    class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700"
                  />
                  <div class="min-w-0 flex-1">
                    <div class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ role.displayName }}</div>
                    <div class="truncate font-mono text-xs/5 text-gray-500 dark:text-gray-400">{{ role.roleId }}</div>
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

            <p class="mt-4 text-xs/5 text-amber-600 dark:text-amber-400">
              Changes take effect within 5-10 minutes.
            </p>
          }
        </div>

        <!-- Actions -->
        <div class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="save()"
            [disabled]="saving() || loading()"
            class="inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            {{ saving() ? 'Saving…' : 'Save Changes' }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }

    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.97);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
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
