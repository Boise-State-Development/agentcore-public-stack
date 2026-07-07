import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroDocumentText, heroChevronDown } from '@ng-icons/heroicons/outline';
import { EntryType } from '../models/memory-space.model';
import { MemorySpaceService } from '../services/memory-space.service';

export interface EntryDialogData {
  spaceId: string;
  /** Present in view/edit mode; absent to create a new entry. */
  slug?: string;
  /** Prefill for edit mode so the body/type/description show before the read resolves. */
  type?: EntryType;
  description?: string;
  /** editor+ may write; a viewer opens read-only. */
  canEdit: boolean;
}

export type EntryDialogResult = { action: 'saved' | 'deleted' } | undefined;

/**
 * View, edit, or create a single Memory Space entry (one markdown file).
 * Editors get a slug/type/description/body form; viewers get a read-only body.
 */
@Component({
  selector: 'app-entry-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [provideIcons({ heroXMark, heroDocumentText, heroChevronDown })],
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
        class="dialog-panel relative transform overflow-hidden rounded-2xl border border-gray-200 bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-2xl sm:p-6 dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="entry-dialog-title"
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
            <ng-icon name="heroDocumentText" class="size-6 text-blue-600 dark:text-blue-400" aria-hidden="true" />
          </div>
          <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
            <h3 id="entry-dialog-title" class="text-base/7 font-semibold text-gray-900 dark:text-white">
              {{ isCreate() ? 'New entry' : (canEdit ? 'Edit entry' : 'Entry') }}
            </h3>
            @if (!isCreate()) {
              <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">{{ slug() }}</p>
            }
          </div>
        </div>

        <div class="mt-6 space-y-4">
          @if (isCreate()) {
            <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label for="entry-slug" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Slug</label>
                <input
                  id="entry-slug"
                  type="text"
                  [ngModel]="slug()"
                  (ngModelChange)="slug.set($event)"
                  placeholder="e.g. jane-doe"
                  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                />
              </div>
              <div>
                <label for="entry-type" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Type</label>
                <div class="relative mt-1 inline-flex w-full">
                  <select
                    id="entry-type"
                    [ngModel]="type()"
                    (ngModelChange)="type.set($event)"
                    class="block w-full appearance-none rounded-2xl border border-gray-300 bg-white py-2 pl-3 pr-9 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    <option value="fact">Fact</option>
                    <option value="entity">Entity</option>
                    <option value="episodic">Episodic</option>
                  </select>
                  <ng-icon name="heroChevronDown" class="pointer-events-none absolute right-3 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                </div>
              </div>
            </div>
            <div>
              <label for="entry-desc" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Description</label>
              <input
                id="entry-desc"
                type="text"
                [ngModel]="description()"
                (ngModelChange)="description.set($event)"
                placeholder="One-line summary shown in the index"
                class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              />
            </div>
          }

          <div>
            <label for="entry-body" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Content</label>
            @if (loading()) {
              <div class="mt-1 h-48 animate-pulse rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800"></div>
            } @else {
              <textarea
                id="entry-body"
                [ngModel]="body()"
                (ngModelChange)="body.set($event)"
                [readonly]="!canEdit"
                rows="12"
                class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 read-only:bg-gray-50 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:read-only:bg-gray-900/40"
                placeholder="# Markdown body"
              ></textarea>
            }
          </div>

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
            {{ canEdit ? 'Cancel' : 'Close' }}
          </button>
          @if (canEdit) {
            <button
              type="button"
              (click)="onSave()"
              [disabled]="saving() || !slug().trim()"
              class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              {{ saving() ? 'Saving…' : 'Save entry' }}
            </button>
          }
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
export class EntryDialogComponent {
  protected readonly dialogRef = inject<DialogRef<EntryDialogResult>>(DialogRef);
  protected readonly data = inject<EntryDialogData>(DIALOG_DATA);
  private readonly service = inject(MemorySpaceService);

  protected readonly canEdit = this.data.canEdit;
  protected readonly slug = signal<string>(this.data.slug ?? '');
  protected readonly type = signal<EntryType>(this.data.type ?? 'fact');
  protected readonly description = signal<string>(this.data.description ?? '');
  protected readonly body = signal<string>('');
  protected readonly loading = signal<boolean>(!!this.data.slug);
  protected readonly saving = signal<boolean>(false);
  protected readonly error = signal<string | null>(null);

  protected readonly isCreate = computed<boolean>(() => !this.data.slug);

  constructor() {
    if (this.data.slug) {
      void this.loadBody(this.data.slug);
    }
  }

  private async loadBody(slug: string): Promise<void> {
    this.loading.set(true);
    try {
      const entry = await this.service.readEntry(this.data.spaceId, slug);
      this.body.set(entry.content);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to load entry');
    } finally {
      this.loading.set(false);
    }
  }

  protected async onSave(): Promise<void> {
    const slug = this.slug().trim();
    if (!slug) {
      return;
    }
    this.saving.set(true);
    this.error.set(null);
    try {
      await this.service.upsertEntry(this.data.spaceId, slug, {
        body: this.body(),
        type: this.type(),
        description: this.description(),
      });
      this.dialogRef.close({ action: 'saved' });
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to save entry');
    } finally {
      this.saving.set(false);
    }
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
