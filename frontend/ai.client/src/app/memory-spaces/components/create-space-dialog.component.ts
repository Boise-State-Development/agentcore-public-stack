import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroCircleStack } from '@ng-icons/heroicons/outline';
import { MemorySpaceSummary, SpaceTemplate } from '../models/memory-space.model';
import { MemorySpaceService } from '../services/memory-space.service';

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
  imports: [FormsModule, NgIcon],
  providers: [provideIcons({ heroXMark, heroCircleStack })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative transform overflow-hidden rounded-2xl border border-gray-200 bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-lg sm:p-6 dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-space-title"
        (click)="$event.stopPropagation()"
      >
        <div class="absolute top-3 right-3 hidden sm:block">
          <button
            type="button"
            (click)="onCancel()"
            class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            aria-label="Close dialog"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="sm:flex sm:items-start">
          <div class="mx-auto flex size-12 shrink-0 items-center justify-center rounded-2xl bg-blue-100 sm:mx-0 sm:size-10 dark:bg-blue-500/10">
            <ng-icon name="heroCircleStack" class="size-6 text-blue-600 dark:text-blue-400" aria-hidden="true" />
          </div>
          <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
            <h3 id="create-space-title" class="text-base/7 font-semibold text-gray-900 dark:text-white">
              New memory space
            </h3>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              A named markdown "second brain" an agent reads and maintains.
            </p>
          </div>
        </div>

        <div class="mt-6 space-y-6">
          <div>
            <label for="space-name" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
              Name
            </label>
            <input
              id="space-name"
              type="text"
              [ngModel]="name()"
              (ngModelChange)="name.set($event)"
              placeholder="e.g. Chief of Staff"
              maxlength="200"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
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
                  class="flex w-full flex-col items-start rounded-2xl border px-4 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 aria-checked:border-blue-500 aria-checked:bg-blue-50 dark:aria-checked:border-blue-400 dark:aria-checked:bg-blue-500/10 border-gray-300 hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-700/40"
                >
                  <span class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ tmpl.name }}</span>
                  @if (tmpl.description) {
                    <span class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">{{ tmpl.description }}</span>
                  }
                </button>
              }
            </div>
          </fieldset>

          @if (error()) {
            <div class="rounded-2xl bg-red-50 px-3 py-2 text-sm/6 text-red-800 dark:bg-red-900/20 dark:text-red-400" role="alert">
              {{ error() }}
            </div>
          }
        </div>

        <div class="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
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
            class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            {{ saving() ? 'Creating…' : 'Create space' }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";
    @custom-variant dark (&:where(.dark, .dark *));
    .dialog-backdrop { animation: backdrop-fade-in 200ms ease-out; }
    @keyframes backdrop-fade-in { from { opacity: 0; } to { opacity: 1; } }
    .dialog-panel { animation: dialog-fade-in-up 200ms ease-out; }
    @keyframes dialog-fade-in-up {
      from { opacity: 0; transform: translateY(1rem) scale(0.95); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
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
