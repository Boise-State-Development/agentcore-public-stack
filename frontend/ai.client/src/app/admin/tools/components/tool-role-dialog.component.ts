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
import { AdminToolService } from '../services/admin-tool.service';
import { AdminTool, ToolRoleAssignment } from '../models/admin-tool.model';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AppRole } from '../../roles/models/app-role.model';
import { DialogDismissDirective } from '../../../components/dialog/dialog-dismiss.directive';

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
  imports: [DialogDismissDirective, NgIcon],
  providers: [provideIcons({ heroXMark, heroUserGroup })],
  host: {
    'class': 'block',
    '(keydown.escape)': 'onCancel()'
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 text-center focus:outline-none sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()">
      <div
        class="dialog-panel relative transform overflow-hidden rounded-lg bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-lg sm:p-6 dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        aria-describedby="dialog-description"
      >
        <!-- Close button (top-right) -->
        <div class="absolute top-0 right-0 hidden pt-4 pr-4 sm:block">
          <!-- intentional: shared dialog-convention indigo focus outline, tracked in phase-3-outlier-colors-and-icon-spinners.md, not this doc -->
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-md bg-white text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-primary-600 dark:bg-gray-800 dark:hover:text-gray-300 dark:focus:outline-white"
            aria-label="Close dialog"
          >
            <span class="sr-only">Close</span>
            <ng-icon name="heroXMark" class="size-6" aria-hidden="true" />
          </button>
        </div>

        <!-- Header with Icon -->
        <div class="sm:flex sm:items-start">
          <div class="mx-auto flex size-12 shrink-0 items-center justify-center rounded-full bg-primary-100 sm:mx-0 sm:size-10 dark:bg-primary-500/10">
            <ng-icon name="heroUserGroup" class="size-6 text-primary-accessible dark:text-primary-accessible-dark" aria-hidden="true" />
          </div>
          <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
            <h3 id="dialog-title" class="text-base font-semibold text-gray-900 dark:text-white">
              Manage Role Access
            </h3>
            <div class="mt-2">
              <p class="text-sm text-gray-500 dark:text-gray-400">
                Select which roles should have access to <span class="font-medium">{{ data.tool.displayName }}</span>.
              </p>
            </div>
          </div>
        </div>

        <!-- Content -->
        <div id="dialog-description" class="mt-4 max-h-72 overflow-y-auto">
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
                  [class.bg-primary-50]="selectedRoleIds().has(role.roleId)"
                  [class.dark:bg-primary-900/20]="selectedRoleIds().has(role.roleId)"
                >
                  <input
                    type="checkbox"
                    [checked]="selectedRoleIds().has(role.roleId)"
                    (change)="toggleRole(role.roleId)"
                    class="size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700"
                  />
                  <div class="flex-1 min-w-0">
                    <div class="font-medium text-gray-900 dark:text-white">{{ role.displayName }}</div>
                    <div class="text-sm text-gray-500 dark:text-gray-400 truncate">{{ role.roleId }}</div>
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
        </div>

        <!-- Actions -->
        <div class="mt-5 sm:mt-4 sm:flex sm:flex-row-reverse">
          <button
            type="button"
            (click)="save()"
            [disabled]="saving() || loading()"
            class="inline-flex w-full justify-center rounded-md bg-primary-600 px-3 py-2 text-sm font-semibold text-white shadow-xs hover:bg-primary-500 sm:ml-3 sm:w-auto dark:bg-primary-500 dark:shadow-none dark:hover:bg-primary-400 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {{ saving() ? 'Saving...' : 'Save Changes' }}
          </button>
          <button
            type="button"
            (click)="onCancel()"
            class="mt-3 inline-flex w-full justify-center rounded-md bg-white px-3 py-2 text-sm font-semibold text-gray-900 shadow-xs inset-ring-1 inset-ring-gray-300 hover:bg-gray-50 sm:mt-0 sm:w-auto dark:bg-white/10 dark:text-white dark:shadow-none dark:inset-ring-white/5 dark:hover:bg-white/20"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    /* Backdrop fade-in animation */
    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from {
        opacity: 0;
      }
      to {
        opacity: 1;
      }
    }

    /* Dialog panel fade-in-up animation */
    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.95);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `
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

