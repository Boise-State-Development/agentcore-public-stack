import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  OnInit,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroUserGroup } from '@ng-icons/heroicons/outline';
import { AdminToolService } from '../services/admin-tool.service';
import { AdminTool, ToolRoleAssignment } from '../models/admin-tool.model';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AppRole } from '../../roles/models/app-role.model';
import { DialogDescriptionDirective } from '../../../components/dialog/dialog-description.directive';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

/**
 * Data passed to the tool role dialog.
 */
export interface ToolRoleDialogData {
  tool: AdminTool;
}

/**
 * Result returned when the dialog is closed.
 * Returns the selected role IDs if saved, or undefined if cancelled.
 */
export type ToolRoleDialogResult = string[] | undefined;

@Component({
  selector: 'app-tool-role-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDescriptionDirective, DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroUserGroup })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Manage Role Access" (closed)="onCancel()">
      <div
        dialogIcon
        class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-gray-100 dark:bg-gray-700"
      >
        <ng-icon name="heroUserGroup" class="size-5 text-primary-accessible dark:text-primary-50" aria-hidden="true" />
      </div>

      <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Select which roles should have access to <span class="font-medium">{{ data.tool.displayName }}</span>.
      </p>

      @if (loading()) {
        <div class="flex items-center justify-center py-8">
          <div class="animate-spin rounded-full size-8 border-4 border-gray-300 dark:border-gray-600 border-t-primary-600"></div>
        </div>
      } @else {
        @if (data.tool.isPublic) {
          <div class="mb-4 p-3 bg-state-success-50 dark:bg-state-success-900/20 border border-state-success-200 dark:border-state-success-800 rounded-md">
            <p class="text-sm text-state-success-800 dark:text-state-success-200">
              This tool is marked as public and is available to all authenticated users.
            </p>
          </div>
        }

        <div class="space-y-2">
          @for (role of allRoles(); track role.roleId) {
            <label
              class="flex items-center gap-3 p-3 border rounded-md hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer transition-colors dark:border-gray-600"
              [class.border-primary-500]="selectedRoleIds().has(role.roleId)"
              [class.dark:border-primary-400]="selectedRoleIds().has(role.roleId)"
              [class.bg-gray-100]="selectedRoleIds().has(role.roleId)"
              [class.dark:bg-gray-700]="selectedRoleIds().has(role.roleId)"
            >
              <input
                type="checkbox"
                [checked]="selectedRoleIds().has(role.roleId)"
                (change)="toggleRole(role.roleId)"
                class="size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700"
              />
              <div class="flex-1 min-w-0">
                <div class="font-medium text-gray-900 dark:text-white">{{ role.displayName }}</div>
                <div class="text-sm text-gray-600 dark:text-gray-300 truncate">{{ role.roleId }}</div>
              </div>
              @if (currentAssignments().has(role.roleId)) {
                <span class="text-xs text-gray-400 dark:text-gray-500 shrink-0">
                  {{ getGrantType(role.roleId) }}
                </span>
              }
            </label>
          }
        </div>

        @if (allRoles().length === 0) {
          <p class="text-center text-gray-500 dark:text-gray-400 py-8">
            No roles available. Create roles first.
          </p>
        }

        <!-- Info notice -->
        <p class="mt-4 text-xs text-state-warning-600 dark:text-state-warning-400">
          Changes take effect within 5-10 minutes.
        </p>
      }

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="onCancel()"
          class="inline-flex justify-center rounded-2xl bg-white px-3 py-2 text-sm font-semibold text-gray-900 shadow-xs inset-ring-1 inset-ring-gray-300 hover:bg-gray-50 dark:bg-white/10 dark:text-white dark:shadow-none dark:inset-ring-white/5 dark:hover:bg-white/20"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="save()"
          [disabled]="saving() || loading()"
          class="inline-flex justify-center rounded-2xl bg-primary-accessible px-3 py-2 text-sm font-semibold text-white shadow-xs hover:brightness-95 dark:shadow-none disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ saving() ? 'Saving...' : 'Save Changes' }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class ToolRoleDialogComponent implements OnInit {
  protected readonly dialogRef = inject(DialogRef<ToolRoleDialogResult>);
  protected readonly data = inject<ToolRoleDialogData>(DIALOG_DATA);

  private adminToolService = inject(AdminToolService);
  private appRolesService = inject(AppRolesService);

  loading = signal(true);
  saving = signal(false);
  allRoles = signal<AppRole[]>([]);
  currentAssignments = signal<Map<string, ToolRoleAssignment>>(new Map());
  selectedRoleIds = signal<Set<string>>(new Set());

  async ngOnInit(): Promise<void> {
    this.loading.set(true);
    try {
      // Load all roles and current assignments in parallel
      const [rolesResponse, assignments] = await Promise.all([
        this.appRolesService.fetchRoles(),
        this.adminToolService.getToolRoles(this.data.tool.toolId),
      ]);

      // Filter out system_admin role from the list
      this.allRoles.set(
        rolesResponse.roles.filter(r => r.roleId !== 'system_admin')
      );

      const assignmentMap = new Map<string, ToolRoleAssignment>();
      for (const a of assignments) {
        assignmentMap.set(a.roleId, a);
      }
      this.currentAssignments.set(assignmentMap);

      // Initialize selected with direct grants only
      const directGrants = assignments
        .filter(a => a.grantType === 'direct')
        .map(a => a.roleId);
      this.selectedRoleIds.set(new Set(directGrants));
    } catch (error) {
      console.error('Error loading data:', error);
    } finally {
      this.loading.set(false);
    }
  }

  toggleRole(roleId: string): void {
    this.selectedRoleIds.update(set => {
      const newSet = new Set(set);
      if (newSet.has(roleId)) {
        newSet.delete(roleId);
      } else {
        newSet.add(roleId);
      }
      return newSet;
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

  async save(): Promise<void> {
    this.saving.set(true);
    try {
      const roleIds = Array.from(this.selectedRoleIds());
      this.dialogRef.close(roleIds);
    } finally {
      this.saving.set(false);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}

