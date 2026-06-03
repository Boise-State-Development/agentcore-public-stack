import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPencil, heroTrash, heroPlus, heroCheckCircle, heroXCircle } from '@ng-icons/heroicons/outline';
import { AdminSystemPromptsService } from './services/admin-system-prompts.service';
import { SystemPromptAdmin } from './models/system-prompt-admin.model';
import { ToastService } from '../../services/toast/toast.service';

@Component({
  selector: 'app-manage-system-prompts-page',
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({ heroPencil, heroTrash, heroPlus, heroCheckCircle, heroXCircle }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
      <div class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Conversation Modes</h1>
          <p class="mt-1 text-gray-600 dark:text-gray-400">
            Manage custom system prompt instructions users can opt into per conversation.
            Prompt text is never shown to users — only the name and description.
          </p>
        </div>
        <a
          routerLink="/admin/system-prompts/new"
          class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
        >
          <ng-icon name="heroPlus" class="size-5" />
          New prompt
        </a>
      </div>

      @if (loadError()) {
        <div class="mb-4 rounded-sm border border-red-300 bg-red-50 p-4 text-sm/6 text-red-700 dark:border-red-700 dark:bg-red-900/20 dark:text-red-300">
          Failed to load prompts. {{ loadError() }}
        </div>
      }

      @if (prompts().length === 0 && !isLoading()) {
        <div class="rounded-sm border border-gray-300 bg-white p-12 text-center dark:border-gray-600 dark:bg-gray-800">
          <p class="text-base/7 text-gray-500 dark:text-gray-400">No conversation modes yet.</p>
          <a
            routerLink="/admin/system-prompts/new"
            class="mt-4 inline-flex items-center gap-2 text-sm/6 font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
          >
            Add the first one →
          </a>
        </div>
      } @else {
        <div class="space-y-3">
          @for (prompt of prompts(); track prompt.prompt_id) {
            <div class="flex items-start justify-between gap-4 rounded-sm border border-gray-300 bg-white p-4 dark:border-gray-600 dark:bg-gray-800">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ prompt.name }}</span>
                  @if (prompt.status === 'enabled') {
                    <span class="shrink-0 inline-flex items-center gap-1 rounded-sm bg-green-100 px-2 py-0.5 text-xs/5 font-medium text-green-700 dark:bg-green-900/40 dark:text-green-300">
                      <ng-icon name="heroCheckCircle" class="size-3.5" />
                      Enabled
                    </span>
                  } @else {
                    <span class="shrink-0 inline-flex items-center gap-1 rounded-sm bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                      <ng-icon name="heroXCircle" class="size-3.5" />
                      Disabled
                    </span>
                  }
                </div>
                <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">{{ prompt.description }}</p>
                <p class="mt-1 truncate text-xs/5 font-mono text-gray-400 dark:text-gray-500">
                  {{ summarize(prompt.prompt_text) }}
                </p>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <a
                  [routerLink]="['/admin/system-prompts/edit', prompt.prompt_id]"
                  class="inline-flex items-center gap-1 rounded-sm border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                  [attr.aria-label]="'Edit ' + prompt.name"
                >
                  <ng-icon name="heroPencil" class="size-4" />
                  <span class="sr-only sm:not-sr-only">Edit</span>
                </a>
                <button
                  type="button"
                  (click)="onDelete(prompt)"
                  class="inline-flex items-center gap-1 rounded-sm border border-red-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500 dark:border-red-500 dark:bg-gray-700 dark:text-red-400 dark:hover:bg-red-900/20"
                  [attr.aria-label]="'Delete ' + prompt.name"
                >
                  <ng-icon name="heroTrash" class="size-4" />
                  <span class="sr-only sm:not-sr-only">Delete</span>
                </button>
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class ManageSystemPromptsPage {
  private readonly service = inject(AdminSystemPromptsService);
  private readonly toast = inject(ToastService);

  constructor() {
    this.service.ensureLoaded();
  }

  protected readonly prompts = computed<SystemPromptAdmin[]>(
    () => this.service.promptsResource.value()?.prompts ?? [],
  );
  protected readonly isLoading = computed(() => this.service.promptsResource.isLoading());
  protected readonly loadError = computed(() => {
    const err = this.service.promptsResource.error();
    if (!err) return null;
    return err instanceof Error ? err.message : String(err);
  });

  protected summarize(text: string): string {
    if (!text) return '';
    const trimmed = text.trim().replace(/\s+/g, ' ');
    return trimmed.length > 140 ? trimmed.slice(0, 140) + '…' : trimmed;
  }

  protected async onDelete(prompt: SystemPromptAdmin): Promise<void> {
    if (!confirm(`Delete "${prompt.name}"? Any sessions using it will fall back to default behaviour.`)) return;
    try {
      await this.service.deletePrompt(prompt.prompt_id);
      this.toast.success('Deleted', `"${prompt.name}" was removed.`);
    } catch (err) {
      console.error('Failed to delete system prompt', err);
      this.toast.error('Could not delete prompt', 'Please try again.');
    }
  }
}
