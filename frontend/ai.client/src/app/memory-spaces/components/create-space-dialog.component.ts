import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCircleStack } from '@ng-icons/heroicons/outline';
import { MemorySpaceSummary, SpaceTemplate } from '../models/memory-space.model';
import { MemorySpaceService } from '../services/memory-space.service';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';

export interface CreateSpaceDialogData {
  templates: SpaceTemplate[];
}

/** The created space, or `undefined` if cancelled. */
export type CreateSpaceDialogResult = MemorySpaceSummary | undefined;

/**
 * Create a Memory Space from a template. Collects a name + template choice,
 * calls the service, and closes with the created space so the parent can
 * navigate straight into it.
 */
@Component({
  selector: 'app-create-space-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, FormsModule, NgIcon],
  providers: [provideIcons({ heroCircleStack })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="New memory space"
      description='A named markdown "second brain" an agent reads and maintains.'
      (closed)="onCancel()"
    >
      <div
        dialogIcon
        class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-gray-100 dark:bg-gray-700"
      >
        <ng-icon name="heroCircleStack" class="size-6 text-primary-accessible dark:text-primary-50" aria-hidden="true" />
      </div>

      <div class="space-y-6">
        <div>
          <label for="space-name" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            Name
          </label>
          <input
            id="space-name"
            type="text"
            cdkFocusInitial
            [ngModel]="name()"
            (ngModelChange)="name.set($event)"
            placeholder="e.g. Chief of Staff"
            maxlength="200"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <fieldset>
          <legend class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Template</legend>
          <div class="mt-2 space-y-2" role="radiogroup" aria-label="Template">
            @for (tmpl of data.templates; track tmpl.templateId) {
              <button
                type="button"
                role="radio"
                [attr.aria-checked]="template() === tmpl.templateId"
                (click)="template.set(tmpl.templateId)"
                class="flex w-full flex-col items-start rounded-2xl border px-4 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 aria-checked:border-primary-500 aria-checked:bg-gray-100 dark:aria-checked:border-primary-400 dark:aria-checked:bg-gray-700 border-gray-300 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-700/40"
              >
                <span class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ tmpl.name }}</span>
                @if (tmpl.description) {
                  <span class="mt-0.5 text-xs/5 text-gray-600 dark:text-gray-300">{{ tmpl.description }}</span>
                }
              </button>
            }
          </div>
        </fieldset>

        @if (error()) {
          <div class="rounded-2xl bg-state-danger-50 px-3 py-2 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-400" role="alert">
            {{ error() }}
          </div>
        }
      </div>

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="onCreate()"
          [disabled]="saving() || !name().trim()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {{ saving() ? 'Creating…' : 'Create space' }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class CreateSpaceDialogComponent {
  protected readonly dialogRef = inject<DialogRef<CreateSpaceDialogResult>>(DialogRef);
  protected readonly data = inject<CreateSpaceDialogData>(DIALOG_DATA);
  private readonly service = inject(MemorySpaceService);

  protected readonly name = signal<string>('');
  protected readonly template = signal<string>(this.data.templates[0]?.templateId ?? 'blank');
  protected readonly saving = signal<boolean>(false);
  protected readonly error = signal<string | null>(null);

  protected async onCreate(): Promise<void> {
    const name = this.name().trim();
    if (!name) {
      return;
    }
    this.saving.set(true);
    this.error.set(null);
    try {
      const space = await this.service.createSpace({ name, template: this.template() });
      this.dialogRef.close(space);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to create space');
    } finally {
      this.saving.set(false);
    }
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
