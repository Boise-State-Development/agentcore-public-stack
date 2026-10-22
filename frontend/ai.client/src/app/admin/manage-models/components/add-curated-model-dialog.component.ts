import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { CuratedModel } from '../models/curated-models';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

/**
 * Data passed to the add-curated-model dialog.
 */
export interface AddCuratedModelDialogData {
  model: CuratedModel;
}

/**
 * Result returned when the dialog closes.
 * - `string[]` — admin confirmed; these role IDs should be applied to the
 *   curated template before POSTing.
 * - `undefined` — admin cancelled.
 */
export type AddCuratedModelDialogResult = string[] | undefined;

@Component({
  selector: 'app-add-curated-model-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      [title]="'Add ' + data.model.template.modelName"
      description="Select which roles can access this model. You can change this later from the model's edit page."
      (closed)="onCancel()"
    >
      @if (rolesResource.isLoading()) {
        <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading roles…</p>
      } @else if (rolesResource.error()) {
        <p class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">
          Failed to load roles. Please refresh the page.
        </p>
      } @else if (availableRoles().length === 0) {
        <p class="text-sm/6 text-state-warning-600 dark:text-state-warning-400">
          No roles configured. Create roles in Admin &gt; Roles first.
        </p>
      } @else {
        <div class="mb-2 flex items-center justify-between text-xs/5">
          <span class="font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Allowed roles
          </span>
          <div class="flex gap-3">
            <button
              type="button"
              (click)="selectAll()"
              class="font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
            >
              Select all
            </button>
            <button
              type="button"
              (click)="clearAll()"
              class="font-medium text-gray-500 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:text-white"
            >
              Clear
            </button>
          </div>
        </div>
        <!-- Focus starts on the first role: picking roles is the whole job of this dialog. -->
        <div class="flex flex-wrap gap-2">
          @for (role of availableRoles(); track role.roleId; let first = $first) {
            <button
              type="button"
              [attr.cdkFocusInitial]="first ? '' : null"
              (click)="toggleRole(role.roleId)"
              [attr.aria-pressed]="isSelected(role.roleId)"
              [class.bg-primary-600]="isSelected(role.roleId)"
              [class.text-white]="isSelected(role.roleId)"
              [class.bg-gray-100]="!isSelected(role.roleId)"
              [class.text-gray-700]="!isSelected(role.roleId)"
              [class.dark:bg-primary-500]="isSelected(role.roleId)"
              [class.dark:bg-gray-700]="!isSelected(role.roleId)"
              [class.dark:text-gray-300]="!isSelected(role.roleId)"
              class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
              [title]="role.description"
            >
              {{ role.displayName }}
            </button>
          }
        </div>
        @if (selectedRoleIds().size === 0) {
          <p class="mt-3 text-xs/5 text-state-warning-600 dark:text-state-warning-400">
            Select at least one role so users can see this model.
          </p>
        }
      }

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="confirm()"
          [disabled]="!canConfirm()"
          class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:brightness-110"
        >
          Add to models
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class AddCuratedModelDialogComponent {
  protected readonly dialogRef = inject(DialogRef<AddCuratedModelDialogResult>);
  protected readonly data = inject<AddCuratedModelDialogData>(DIALOG_DATA);

  private appRolesService = inject(AppRolesService);

  readonly rolesResource = this.appRolesService.rolesResource;
  readonly availableRoles = computed(() => this.appRolesService.getEnabledRoles());

  readonly selectedRoleIds = signal<Set<string>>(new Set());
  readonly canConfirm = computed(() => this.selectedRoleIds().size > 0);

  isSelected(roleId: string): boolean {
    return this.selectedRoleIds().has(roleId);
  }

  toggleRole(roleId: string): void {
    this.selectedRoleIds.update(set => {
      const next = new Set(set);
      if (next.has(roleId)) next.delete(roleId);
      else next.add(roleId);
      return next;
    });
  }

  selectAll(): void {
    this.selectedRoleIds.set(new Set(this.availableRoles().map(r => r.roleId)));
  }

  clearAll(): void {
    this.selectedRoleIds.set(new Set());
  }

  confirm(): void {
    if (!this.canConfirm()) return;
    this.dialogRef.close(Array.from(this.selectedRoleIds()));
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
