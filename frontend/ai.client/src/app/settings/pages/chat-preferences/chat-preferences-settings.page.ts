import {
  Component,
  ChangeDetectionStrategy,
  inject,
  computed,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSparkles, heroChatBubbleLeftRight, heroChevronRight, heroBugAnt } from '@ng-icons/heroicons/outline';
import { HttpErrorResponse } from '@angular/common/http';
import { ModelService } from '../../../session/services/model/model.service';
import { MAX_PERSONAL_INSTRUCTIONS, UserSettingsService } from '../../../services/user-settings.service';
import { LocalSettingsService } from '../../../services/local-settings.service';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { isRetiring } from '../../../shared/utils/retirement';

@Component({
  selector: 'app-chat-preferences-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, SpinnerComponent],
  providers: [
    provideIcons({ heroSparkles, heroChatBubbleLeftRight, heroChevronRight, heroBugAnt }),
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
                <app-spinner size="sm" label="Loading models" />
                Loading models...
              </div>
            } @else {
              <select
                class="block w-full rounded-sm border-0 bg-white py-1.5 pl-3 pr-10 text-sm/6 text-gray-900 shadow-xs ring-1 ring-gray-300 focus:ring-2 focus:ring-primary-600 dark:bg-white/5 dark:text-white dark:ring-white/10 dark:focus:ring-primary-500"
                aria-label="Default model"
                (change)="onModelChange($event)"
              >
                <!--
                  We bind [selected] on each <option> rather than [value] on
                  the <select>. Native <select>.value is a one-time DOM
                  property write: if Angular evaluates it before @for has
                  rendered the matching <option> (same change-detection
                  tick), the browser silently resets the select to the
                  first option and never resyncs when options arrive. With
                  [selected], the binding fires as each option mounts, so
                  the saved modelId reliably wins regardless of which
                  data source — settings or model list — resolves first
                  (#161).
                -->
                <option value="" [selected]="currentDefaultModelId() === ''">No default (use first available)</option>
                @for (model of modelService.availableModels(); track model.id) {
                  <!-- A model being retired can stay the default but can't become
                       it (docs/specs/model-retirement.md §7). -->
                  <option
                    [value]="model.modelId"
                    [selected]="model.modelId === currentDefaultModelId()"
                    [disabled]="isRetiring(model) && model.modelId !== currentDefaultModelId()"
                  >{{ model.modelName }} ({{ model.providerName }}){{ isRetiring(model) ? ' — being retired' : '' }}</option>
                }
              </select>
              @if (retiredDefaultNotice(); as notice) {
                <p class="mt-2 text-xs/5 text-state-warning-700 dark:text-state-warning-300">{{ notice }}</p>
              }
            }
            @if (saving()) {
              <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">Saving...</p>
            }
            @if (saveError()) {
              <p class="mt-2 text-xs text-state-danger-600 dark:text-state-danger-400">{{ saveError() }}</p>
            }
          </div>
        </div>
      </div>

      <!-- Personal instructions -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <form class="p-6" (submit)="$event.preventDefault(); savePersonalInstructions()">
          <label for="personal-instructions" class="text-sm/6 font-medium text-gray-900 dark:text-white">
            Personal instructions
          </label>
          <p id="personal-instructions-help" class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            How you’d like the assistant to work with you, in every conversation, including in agents and projects.
            Where an agent’s or a project’s own instructions disagree, theirs win.
          </p>
          <textarea
            id="personal-instructions"
            rows="5"
            [value]="personalValue()"
            (input)="personalDraft.set($any($event.target).value)"
            [disabled]="!settingsLoaded()"
            [attr.maxlength]="personalMax"
            aria-describedby="personal-instructions-help personal-instructions-count"
            placeholder="For example: I teach undergraduate chemistry. Keep answers brief and use SI units."
            class="mt-3 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-400"
          ></textarea>
          <div class="mt-2 flex flex-wrap items-center justify-between gap-3">
            <p id="personal-instructions-count" class="text-xs/5 text-gray-600 dark:text-gray-400">
              {{ personalValue().length.toLocaleString() }} / {{ personalMax.toLocaleString() }} characters
            </p>
            <div class="flex items-center gap-3">
              @if (personalStatus(); as status) {
                <p
                  role="status"
                  class="text-xs/5"
                  [class]="status.error ? 'text-state-danger-600 dark:text-state-danger-400' : 'text-state-success-700 dark:text-state-success-400'"
                >{{ status.text }}</p>
              }
              <button
                type="submit"
                [disabled]="!personalDirty() || personalSaving()"
                class="rounded-2xl bg-primary-accessible px-3.5 py-1.5 text-sm/6 font-semibold text-white shadow-xs transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {{ personalSaving() ? 'Saving…' : 'Save' }}
              </button>
            </div>
          </div>
        </form>
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
          <div class="group relative inline-flex w-11 shrink-0 rounded-full bg-gray-200 p-0.5 inset-ring inset-ring-gray-900/5 outline-offset-2 outline-primary-600 transition-colors duration-200 ease-in-out has-checked:bg-primary-600 has-focus-visible:outline-2 dark:bg-white/5 dark:inset-ring-white/10 dark:outline-primary-500 dark:has-checked:bg-primary-500">
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

      <!-- Show Debug Output toggle -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <div class="flex items-center justify-between gap-4 p-6">
          <div class="flex items-start gap-3">
            <div class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-gray-100 dark:bg-white/10">
              <ng-icon name="heroBugAnt" class="size-4 text-gray-500 dark:text-gray-400" />
            </div>
            <div>
              <label for="show-debug-output" class="text-sm/6 font-medium text-gray-900 dark:text-white">
                Show debug output
              </label>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Show the full prompt sent to the model instead of the original message.
              </p>
            </div>
          </div>

          <!-- Toggle -->
          <div class="group relative inline-flex w-11 shrink-0 rounded-full bg-gray-200 p-0.5 inset-ring inset-ring-gray-900/5 outline-offset-2 outline-primary-600 transition-colors duration-200 ease-in-out has-checked:bg-primary-600 has-focus-visible:outline-2 dark:bg-white/5 dark:inset-ring-white/10 dark:outline-primary-500 dark:has-checked:bg-primary-500">
            <span class="size-5 rounded-full bg-white shadow-xs ring-1 ring-gray-900/5 transition-transform duration-200 ease-in-out group-has-checked:translate-x-5"></span>
            <input
              id="show-debug-output"
              type="checkbox"
              [checked]="localSettings.showDebugOutput()"
              (change)="onDebugOutputToggle($event)"
              aria-label="Show debug output"
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
                View and manage what the agent remembers about you.
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

  protected readonly isRetiring = isRetiring;

  /**
   * A saved default on a model that has since been retired no longer matches
   * any option, so the select reads "No default" — say what actually happens.
   */
  readonly retiredDefaultNotice = computed(() => {
    const id = this.currentDefaultModelId();
    const successor = this.modelService.successorFor(id);
    if (!successor) return null;
    const name = this.modelService.modelNameFor(id) ?? 'Your default model';
    return `${name} has been retired, so ${successor.modelName} answers in its place. Choose a new default to stop seeing this.`;
  });

  readonly currentDefaultModelId = computed(() => {
    const settings = this.userSettingsService.settingsResource.value();
    const models = this.modelService.availableModels();
    // Wait for both data sources before binding the dropdown value. If we
    // emit the saved modelId before the @for loop has rendered the matching
    // <option>, the browser silently resets the <select> to the first
    // option and Angular won't re-apply [value] when options arrive later
    // because the computed input hasn't changed.
    if (!settings || models.length === 0) return '';
    return settings.defaultModelId ?? '';
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

  // ── Personal instructions ─────────────────────────────────────────────

  protected readonly personalMax = MAX_PERSONAL_INSTRUCTIONS;
  /** What the user has typed since the last save; null = showing the saved value. */
  readonly personalDraft = signal<string | null>(null);
  readonly personalSaving = signal(false);
  readonly personalStatus = signal<{ text: string; error: boolean } | null>(null);

  /** The saved settings, or undefined while loading or after a failed read (reading `value()` then would throw). */
  private readonly savedSettings = computed(() => {
    const settings = this.userSettingsService.settingsResource;
    return settings.error?.() ? undefined : settings.value();
  });
  readonly settingsLoaded = computed(() => this.savedSettings() !== undefined);
  private readonly personalSaved = computed(() => this.savedSettings()?.personalInstructions ?? '');
  readonly personalValue = computed(() => this.personalDraft() ?? this.personalSaved());
  readonly personalDirty = computed(() => {
    const draft = this.personalDraft();
    return draft !== null && draft.trim() !== this.personalSaved().trim();
  });

  async savePersonalInstructions(): Promise<void> {
    if (!this.personalDirty()) return;
    this.personalSaving.set(true);
    this.personalStatus.set(null);
    try {
      // Silent: the result is said right beside the button.
      const saved = await this.userSettingsService.updateSettings(
        { personalInstructions: this.personalValue() },
        { silent: true },
      );
      this.personalDraft.set(null);
      this.personalStatus.set({
        text: saved.personalInstructions ? 'Saved' : 'Cleared',
        error: false,
      });
    } catch (err) {
      const detail = err instanceof HttpErrorResponse && typeof err.error?.detail === 'string' ? err.error.detail : null;
      this.personalStatus.set({ text: detail ?? 'Couldn’t save. Please try again.', error: true });
    } finally {
      this.personalSaving.set(false);
    }
  }

  onTokenCountToggle(event: Event): void {
    const checkbox = event.target as HTMLInputElement;
    this.localSettings.setShowTokenCount(checkbox.checked);
  }

  onDebugOutputToggle(event: Event): void {
    const checkbox = event.target as HTMLInputElement;
    this.localSettings.setShowDebugOutput(checkbox.checked);
  }
}
