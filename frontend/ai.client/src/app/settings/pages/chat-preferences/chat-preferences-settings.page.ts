import {
  Component,
  ChangeDetectionStrategy,
  inject,
  computed,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSparkles, heroChatBubbleLeftRight, heroChevronRight } from '@ng-icons/heroicons/outline';
import { ModelService } from '../../../session/services/model/model.service';
import { UserSettingsService } from '../../../services/user-settings.service';
import { LocalSettingsService } from '../../../services/local-settings.service';

@Component({
  selector: 'app-chat-preferences-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [
    provideIcons({ heroSparkles, heroChatBubbleLeftRight, heroChevronRight }),
  ],
  host: { class: 'block' },
  template: `
    <div class="flex flex-col gap-8">
      <!-- Section header -->
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Chat Preferences</h2>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Configure how you interact with AI models.
        </p>
      </div>

      <!-- Default model -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <div class="p-6">
          <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">Default model</h3>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            Choose which model is selected by default when starting a new conversation.
          </p>

          <div class="mt-4">
            @if (modelService.modelsLoading()) {
              <div class="flex items-center gap-2 text-sm/6 text-gray-500 dark:text-gray-400">
                <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600"></div>
                Loading models...
              </div>
            } @else {
              <select
                class="block w-full rounded-sm border-0 bg-white py-1.5 pl-3 pr-10 text-sm/6 text-gray-900 shadow-xs ring-1 ring-gray-300 focus:ring-2 focus:ring-blue-600 dark:bg-white/5 dark:text-white dark:ring-white/10 dark:focus:ring-blue-500"
                aria-label="Default model"
                [value]="currentDefaultModelId()"
                (change)="onModelChange($event)"
              >
                <option value="">No default (use first available)</option>
                @for (model of modelService.availableModels(); track model.id) {
                  <option [value]="model.modelId">{{ model.modelName }} ({{ model.providerName }})</option>
                }
              </select>
            }
            @if (saving()) {
              <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">Saving...</p>
            }
            @if (saveError()) {
              <p class="mt-2 text-xs text-red-600 dark:text-red-400">{{ saveError() }}</p>
            }
          </div>
        </div>
      </div>

      <!-- Show Token Count toggle -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <div class="flex items-center justify-between gap-4 p-6">
          <div class="flex items-start gap-3">
            <div class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-gray-100 dark:bg-white/10">
              <ng-icon name="heroSparkles" class="size-4 text-gray-500 dark:text-gray-400" />
            </div>
            <div>
              <label for="show-token-count" class="text-sm/6 font-medium text-gray-900 dark:text-white">
                Show token count
              </label>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Display token usage, latency, and cost badges on each message.
              </p>
            </div>
          </div>

          <!-- Toggle -->
          <div class="group relative inline-flex w-11 shrink-0 rounded-full bg-gray-200 p-0.5 inset-ring inset-ring-gray-900/5 outline-offset-2 outline-blue-600 transition-colors duration-200 ease-in-out has-checked:bg-blue-600 has-focus-visible:outline-2 dark:bg-white/5 dark:inset-ring-white/10 dark:outline-blue-500 dark:has-checked:bg-blue-500">
            <span class="size-5 rounded-full bg-white shadow-xs ring-1 ring-gray-900/5 transition-transform duration-200 ease-in-out group-has-checked:translate-x-5"></span>
            <input
              id="show-token-count"
              type="checkbox"
              [checked]="localSettings.showTokenCount()"
              (change)="onTokenCountToggle($event)"
              aria-label="Show token count"
              class="absolute inset-0 size-full cursor-pointer appearance-none focus:outline-hidden"
            />
          </div>
        </div>
      </div>

      <!-- Manage Conversations -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <a
          routerLink="/manage-sessions"
          class="flex items-center justify-between gap-4 p-6 transition-colors hover:bg-gray-50 dark:hover:bg-white/5"
        >
          <div class="flex items-start gap-3">
            <div class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-gray-100 dark:bg-white/10">
              <ng-icon name="heroChatBubbleLeftRight" class="size-4 text-gray-500 dark:text-gray-400" />
            </div>
            <div>
              <span class="text-sm/6 font-medium text-gray-900 dark:text-white">Manage Conversations</span>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Select and delete old conversations.
              </p>
            </div>
          </div>
          <ng-icon name="heroChevronRight" class="size-5 shrink-0 text-gray-400 dark:text-gray-500" />
        </a>
      </div>

      <!-- Memories -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <a
          routerLink="/memories"
          class="flex items-center justify-between gap-4 p-6 transition-colors hover:bg-gray-50 dark:hover:bg-white/5"
        >
          <div class="flex items-start gap-3">
            <div class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-gray-100 dark:bg-white/10">
              <ng-icon name="heroSparkles" class="size-4 text-gray-500 dark:text-gray-400" />
            </div>
            <div>
              <span class="text-sm/6 font-medium text-gray-900 dark:text-white">Memories</span>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                View and manage what the assistant remembers about you.
              </p>
            </div>
          </div>
          <ng-icon name="heroChevronRight" class="size-5 shrink-0 text-gray-400 dark:text-gray-500" />
        </a>
      </div>
    </div>
  `,
})
export class ChatPreferencesSettingsPage {
  readonly modelService = inject(ModelService);
  private userSettingsService = inject(UserSettingsService);
  readonly localSettings = inject(LocalSettingsService);

  readonly saving = signal(false);
  readonly saveError = signal<string | null>(null);

  readonly currentDefaultModelId = computed(() => {
    const settings = this.userSettingsService.settingsResource.value();
    return settings?.defaultModelId ?? '';
  });

  async onModelChange(event: Event): Promise<void> {
    const select = event.target as HTMLSelectElement;
    const modelId = select.value || null;
    this.saving.set(true);
    this.saveError.set(null);

    try {
      await this.userSettingsService.updateSettings({ defaultModelId: modelId });
    } catch {
      this.saveError.set('Failed to save default model. Please try again.');
    } finally {
      this.saving.set(false);
    }
  }

  onTokenCountToggle(event: Event): void {
    const checkbox = event.target as HTMLInputElement;
    this.localSettings.setShowTokenCount(checkbox.checked);
  }
}
