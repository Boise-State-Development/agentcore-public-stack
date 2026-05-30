import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft } from '@ng-icons/heroicons/outline';
import { AdminSystemPromptsService } from './services/admin-system-prompts.service';

const MAX_PROMPT_TEXT = 8000;

@Component({
  selector: 'app-system-prompt-form-page',
  imports: [RouterLink, ReactiveFormsModule, NgIcon],
  providers: [provideIcons({ heroArrowLeft })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="max-w-2xl">
      <a
        routerLink="/admin/system-prompts"
        class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
      >
        <ng-icon name="heroArrowLeft" class="size-4" />
        Back to Conversation Modes
      </a>

      <h1 class="mb-6 text-2xl/8 font-bold text-gray-900 dark:text-white">
        {{ isEdit() ? 'Edit Conversation Mode' : 'New Conversation Mode' }}
      </h1>

      @if (loadError()) {
        <div class="mb-4 rounded-sm border border-red-300 bg-red-50 p-4 text-sm/6 text-red-700 dark:border-red-700 dark:bg-red-900/20 dark:text-red-300">
          {{ loadError() }}
        </div>
      }

      <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-5" novalidate>

        <!-- Name -->
        <div>
          <label for="name" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">
            Name <span aria-hidden="true" class="text-red-500">*</span>
          </label>
          <input
            id="name"
            type="text"
            formControlName="name"
            maxlength="128"
            placeholder="e.g. Guided Learning"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            [class.border-red-500]="form.controls.name.invalid && form.controls.name.touched"
            aria-describedby="name-error"
          />
          @if (form.controls.name.invalid && form.controls.name.touched) {
            <p id="name-error" class="mt-1 text-xs/5 text-red-600 dark:text-red-400" role="alert">Name is required (max 128 characters).</p>
          }
        </div>

        <!-- Description -->
        <div>
          <label for="description" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">
            Description <span aria-hidden="true" class="text-red-500">*</span>
          </label>
          <input
            id="description"
            type="text"
            formControlName="description"
            maxlength="512"
            placeholder="Shown to users in the conversation settings panel"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            [class.border-red-500]="form.controls.description.invalid && form.controls.description.touched"
            aria-describedby="desc-error"
          />
          @if (form.controls.description.invalid && form.controls.description.touched) {
            <p id="desc-error" class="mt-1 text-xs/5 text-red-600 dark:text-red-400" role="alert">Description is required (max 512 characters).</p>
          }
        </div>

        <!-- Prompt Text -->
        <div>
          <label for="prompt_text" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">
            Prompt Text <span aria-hidden="true" class="text-red-500">*</span>
          </label>
          <p class="mb-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
            These instructions are appended to the base system prompt. Users never see this text — only the name and description above.
          </p>
          <textarea
            id="prompt_text"
            formControlName="prompt_text"
            rows="12"
            [maxlength]="maxPromptLength"
            placeholder="Write instructions here..."
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            [class.border-red-500]="form.controls.prompt_text.invalid && form.controls.prompt_text.touched"
            aria-describedby="prompt-error prompt-count"
          ></textarea>
          <div class="mt-1 flex items-start justify-between gap-4">
            @if (form.controls.prompt_text.invalid && form.controls.prompt_text.touched) {
              <p id="prompt-error" class="text-xs/5 text-red-600 dark:text-red-400" role="alert">Prompt text is required (max {{ maxPromptLength }} characters).</p>
            } @else {
              <span></span>
            }
            <span id="prompt-count" class="shrink-0 text-xs/5 text-gray-400 dark:text-gray-500">
              {{ form.controls.prompt_text.value.length }} / {{ maxPromptLength }}
            </span>
          </div>
        </div>

        <!-- Status -->
        <div>
          <label for="status" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Status</label>
          <select
            id="status"
            formControlName="status"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="enabled">Enabled — visible to users</option>
            <option value="disabled">Disabled — hidden from users</option>
          </select>
        </div>

        @if (submitError()) {
          <div class="rounded-sm border border-red-300 bg-red-50 p-3 text-sm/6 text-red-700 dark:border-red-700 dark:bg-red-900/20 dark:text-red-300" role="alert">
            {{ submitError() }}
          </div>
        }

        <div class="flex items-center gap-3 pt-2">
          <button
            type="submit"
            [disabled]="form.invalid || saving()"
            class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            {{ saving() ? 'Saving…' : (isEdit() ? 'Save changes' : 'Create prompt') }}
          </button>
          <a
            routerLink="/admin/system-prompts"
            class="text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
          >
            Cancel
          </a>
        </div>
      </form>
    </div>
  `,
})
export class SystemPromptFormPage implements OnInit {
  private readonly service = inject(AdminSystemPromptsService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly maxPromptLength = MAX_PROMPT_TEXT;
  protected readonly isEdit = signal(false);
  protected readonly saving = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly submitError = signal<string | null>(null);

  protected readonly form = new FormGroup({
    name: new FormControl('', { nonNullable: true, validators: [Validators.required, Validators.maxLength(128)] }),
    description: new FormControl('', { nonNullable: true, validators: [Validators.required, Validators.maxLength(512)] }),
    prompt_text: new FormControl('', { nonNullable: true, validators: [Validators.required, Validators.maxLength(MAX_PROMPT_TEXT)] }),
    status: new FormControl<'enabled' | 'disabled'>('enabled', { nonNullable: true }),
  });

  async ngOnInit(): Promise<void> {
    const promptId = this.route.snapshot.paramMap.get('promptId');
    if (promptId) {
      this.isEdit.set(true);
      try {
        const prompt = await this.service.getPrompt(promptId);
        this.form.setValue({
          name: prompt.name,
          description: prompt.description,
          prompt_text: prompt.prompt_text,
          status: prompt.status,
        });
      } catch (err) {
        this.loadError.set(err instanceof Error ? err.message : 'Failed to load prompt.');
      }
    }
  }

  async onSubmit(): Promise<void> {
    if (this.form.invalid || this.saving()) return;
    this.submitError.set(null);
    this.saving.set(true);

    const data = this.form.getRawValue();
    const promptId = this.route.snapshot.paramMap.get('promptId');

    try {
      if (promptId) {
        await this.service.updatePrompt(promptId, data);
      } else {
        await this.service.createPrompt(data);
      }
      await this.router.navigate(['/admin/system-prompts']);
    } catch (err) {
      this.submitError.set(err instanceof Error ? err.message : 'Failed to save prompt. Please try again.');
    } finally {
      this.saving.set(false);
    }
  }
}
