import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { CuratedModel } from '../models/curated-models';

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
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    'class': 'block',
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
        aria-labelledby="add-curated-title"
        aria-describedby="add-curated-description"
      >
        <!-- Header -->
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="add-curated-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Add {{ data.model.template.modelName }}
            </h2>
            <p id="add-curated-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Select which roles can access this model. You can change this later from the model's edit page.
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
        <div class="px-6 py-4">
          @if (rolesResource.isLoading()) {
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading roles…</p>
          } @else if (rolesResource.error()) {
            <p class="text-sm/6 text-red-600 dark:text-red-400">
              Failed to load roles. Please refresh the page.
            </p>
          } @else if (availableRoles().length === 0) {
            <p class="text-sm/6 text-amber-600 dark:text-amber-400">
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
                  class="font-medium text-blue-600 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
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
            <div class="flex flex-wrap gap-2">
              @for (role of availableRoles(); track role.roleId) {
                <button
                  type="button"
                  (click)="toggleRole(role.roleId)"
                  [attr.aria-pressed]="isSelected(role.roleId)"
                  [class.bg-purple-600]="isSelected(role.roleId)"
                  [class.text-white]="isSelected(role.roleId)"
                  [class.bg-gray-100]="!isSelected(role.roleId)"
                  [class.text-gray-700]="!isSelected(role.roleId)"
                  [class.dark:bg-purple-500]="isSelected(role.roleId)"
                  [class.dark:bg-gray-700]="!isSelected(role.roleId)"
                  [class.dark:text-gray-300]="!isSelected(role.roleId)"
                  class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-purple-500"
                  [title]="role.description"
                >
                  {{ role.displayName }}
                </button>
              }
            </div>
            @if (selectedRoleIds().size === 0) {
              <p class="mt-3 text-xs/5 text-amber-600 dark:text-amber-400">
                Select at least one role so users can see this model.
              </p>
            }
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
            (click)="confirm()"
            [disabled]="!canConfirm()"
            class="inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            Add to models
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
