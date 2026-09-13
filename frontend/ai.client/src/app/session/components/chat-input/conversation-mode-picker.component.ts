import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroSparkles } from '@ng-icons/heroicons/outline';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';

/**
 * Conversation Mode picker for the composer.
 *
 * A Mode is an admin-authored set of instructions applied to **this
 * conversation** — the one genuinely per-conversation control the settings
 * drawer held. When the drawer was retired (step 5 of
 * `docs/specs/customize-surface.md`) its Skills and Tools sections moved to
 * Customize because they were global; Mode could not follow them there without
 * recreating the exact scope lie the whole epic set out to fix. So it moved the
 * other way, into the composer, next to the model and effort controls: those
 * three are the same question — how should *this* conversation run.
 *
 * Replaces the old passive chip, which could display an active mode but never
 * select one (selection lived in the drawer). One control now does both.
 *
 * Renders nothing when no modes exist, which is most deployments — `hasPrompts()`
 * is false until an admin authors one, and an empty picker would be a control
 * that opens onto a single "None" option.
 */
@Component({
  selector: 'app-conversation-mode-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenuTrigger, CdkMenu, CdkMenuItem, NgIcon],
  providers: [provideIcons({ heroCheck, heroSparkles })],
  template: `
    @if (systemPromptsService.hasPrompts()) {
      <div class="relative">
        <button
          type="button"
          [cdkMenuTriggerFor]="modeMenu"
          [cdkMenuPosition]="menuPositions"
          [class]="triggerClass()"
          [attr.aria-label]="triggerLabel()"
        >
          <ng-icon name="heroSparkles" class="size-4 shrink-0" aria-hidden="true" />
          <span class="max-w-28 truncate">{{ activeName() ?? 'Mode' }}</span>
        </button>

        <ng-template #modeMenu>
          <div
            cdkMenu
            class="max-h-96 w-72 overflow-y-auto rounded-md bg-white p-1.5 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
            role="menu"
            aria-label="Conversation mode"
          >
            <button
              type="button"
              cdkMenuItem
              (cdkMenuItemTriggered)="select(null)"
              class="flex w-full items-start justify-between gap-2 rounded-xs px-3 py-2 text-left outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
              role="menuitemradio"
              [attr.aria-checked]="activePromptId() === null"
            >
              <span class="min-w-0">
                <span class="block text-sm/5 text-gray-900 dark:text-white">None</span>
                <span class="block text-xs/5 text-gray-500 dark:text-gray-400">
                  Standard assistant behaviour.
                </span>
              </span>
              @if (activePromptId() === null) {
                <ng-icon
                  name="heroCheck"
                  class="mt-0.5 size-4 shrink-0 text-primary-600 dark:text-primary-400"
                  aria-hidden="true"
                />
              }
            </button>

            @for (prompt of systemPromptsService.prompts(); track prompt.prompt_id) {
              <button
                type="button"
                cdkMenuItem
                (cdkMenuItemTriggered)="select(prompt.prompt_id)"
                class="flex w-full items-start justify-between gap-2 rounded-xs px-3 py-2 text-left outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
                role="menuitemradio"
                [attr.aria-checked]="activePromptId() === prompt.prompt_id"
              >
                <span class="min-w-0">
                  <span class="block text-sm/5 text-gray-900 dark:text-white">{{ prompt.name }}</span>
                  <span class="block text-xs/5 text-gray-500 dark:text-gray-400">
                    {{ prompt.description }}
                  </span>
                </span>
                @if (activePromptId() === prompt.prompt_id) {
                  <ng-icon
                    name="heroCheck"
                    class="mt-0.5 size-4 shrink-0 text-primary-600 dark:text-primary-400"
                    aria-hidden="true"
                  />
                }
              </button>
            }
          </div>
        </ng-template>
      </div>
    }
  `,
})
export class ConversationModePickerComponent {
  protected readonly systemPromptsService = inject(SystemPromptsService);

  /** Session the selection persists against. Null before the first turn. */
  readonly sessionId = input<string | null>(null);

  protected readonly activePromptId = this.systemPromptsService.activePromptId;

  protected readonly activeName = computed<string | null>(() => {
    const active = this.systemPromptsService.activePrompt();
    return active?.name ?? null;
  });

  /**
   * The trigger carries the brand accent only while a mode is on. Off, it has to
   * sit quietly beside the attach and voice buttons — an always-accented control
   * would read as a warning about a state the user has not chosen.
   */
  protected readonly triggerClass = computed(() => {
    const base =
      'flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm/5 transition-colors ' +
      'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)]';
    return this.activeName()
      ? `${base} bg-primary-50 text-primary-700 hover:bg-primary-100 dark:bg-primary-900/30 dark:text-primary-300 dark:hover:bg-primary-900/50`
      : `${base} text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-300`;
  });

  protected readonly triggerLabel = computed(() => {
    const name = this.activeName();
    return name ? `Conversation mode: ${name}. Change it.` : 'Set a conversation mode';
  });

  protected menuPositions: ConnectedPosition[] = [
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -8 },
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
  ];

  protected select(promptId: string | null): void {
    this.systemPromptsService
      .setActivePrompt(this.sessionId(), promptId)
      .catch(err => console.error('Failed to persist conversation mode:', err));
  }
}
