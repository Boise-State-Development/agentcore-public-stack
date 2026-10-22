import { Component, ChangeDetectionStrategy, computed, inject, input, signal } from '@angular/core';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroChevronRight, heroLockClosed } from '@ng-icons/heroicons/outline';
import { ModelService } from '../../session/services/model/model.service';
import { SessionService } from '../../session/services/session/session.service';
import {
  ManagedModel,
  effortLevelLabel,
} from '../../admin/manage-models/models/managed-model.model';
import { ModelOptionComponent } from './components/model-option.component';
import { PromptCacheGuardService } from '../../session/services/prompt-cache/prompt-cache-guard.service';

@Component({
  selector: 'app-model-dropdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenuTrigger, CdkMenu, CdkMenuItem, NgIcon, ModelOptionComponent],
  providers: [provideIcons({ heroCheck, heroChevronRight, heroLockClosed })],
  template: `
    <div class="relative">
      @if (modelService.agentModelLocked()) {
        <!-- Agent-dictated: the active agent pins this model; the picker is locked. -->
        <div
          class="flex items-center rounded-lg text-gray-500 dark:text-gray-400"
          [class]="lockedClass()"
          title="This agent runs on a fixed model"
        >
          <ng-icon name="heroLockClosed" class="size-3.5 shrink-0" aria-hidden="true" />
          <span>{{ modelService.selectedModel().modelName || 'Loading...' }}</span>
          <span class="sr-only">(set by this agent)</span>
        </div>
      } @else {
        <button
          type="button"
          [cdkMenuTriggerFor]="modelMenu"
          [cdkMenuPosition]="menuPositions()"
          (cdkMenuOpened)="onMenuOpened()"
          (cdkMenuClosed)="onMenuClosed()"
          [class]="triggerSizeClass()"
          class="group flex items-center rounded-lg text-gray-600 transition-colors hover:bg-gray-200 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)] dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-300"
          aria-label="Select model"
        >
          <span>{{ modelService.selectedModel().modelName || 'Loading...' }}</span>
          @if (activeEffortLabel(); as effort) {
            <!-- The active effort rides in the trigger so it's visible without
                 opening the menu — it changes cost and latency, not just output. -->
            <span class="text-gray-500 group-hover:text-gray-600 dark:text-gray-500 dark:group-hover:text-gray-500">{{ effort }}</span>
          }
          <svg
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
            class="transition-transform"
            [class]="size() === 'compact' ? 'size-3.5' : 'size-4'"
            [class.rotate-180]="isMenuOpen()"
          >
            <path
              fill-rule="evenodd"
              d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
              clip-rule="evenodd"
            />
          </svg>
        </button>
      }

      <ng-template #modelMenu>
        <div
          cdkMenu
          class="w-72 rounded-md bg-white p-1.5 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10 animate-in fade-in slide-in-from-top-1 duration-200"
          role="menu"
          aria-orientation="vertical"
        >
          @if (modelService.modelsLoading()) {
            <div class="px-3 py-2 text-sm/5 text-gray-500 dark:text-gray-400">
              Loading models...
            </div>
          } @else if (modelService.modelsError()) {
            <div class="px-3 py-2 text-sm/5 text-state-danger-600 dark:text-state-danger-400">
              {{ modelService.modelsError() }}
            </div>
          } @else if (modelService.availableModels().length === 0) {
            <!-- Show default model option when no models are available -->
            <button
              cdkMenuItem
              type="button"
              class="flex w-full items-center justify-between gap-2 rounded-xs px-3 py-2 text-sm/5 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
              role="menuitem"
              disabled
            >
              <div class="min-w-0 text-left">
                <div class="truncate font-medium">
                  {{ modelService.selectedModel().modelName || 'System Default' }}
                </div>
                <div class="truncate text-xs/4 text-gray-500 dark:text-gray-400">
                  Using backend default
                </div>
              </div>
              <ng-icon
                name="heroCheck"
                class="size-4 shrink-0 text-primary-500 dark:text-slate-400"
                aria-hidden="true"
              />
            </button>
          } @else {
            @for (model of modelService.featuredModels(); track model.modelId) {
              <app-model-option
                cdkMenuItem
                [model]="model"
                [selected]="isSelected(model)"
                [successorName]="modelService.modelNameFor(model.replacedBy)"
                (cdkMenuItemTriggered)="selectModel(model)"
              />
            }

            @if (effortControl(); as effort) {
              <div class="my-1.5 border-t border-gray-200 dark:border-gray-700"></div>
              <button
                cdkMenuItem
                type="button"
                [cdkMenuTriggerFor]="effortMenu"
                [cdkMenuPosition]="submenuPositions"
                class="flex w-full items-center justify-between gap-2 rounded-xs px-3 py-2 text-sm/5 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
                role="menuitem"
              >
                <span>Effort</span>
                <span class="flex items-center gap-1">
                  @if (activeEffortLabel(); as label) {
                    <span class="text-xs/4 text-gray-500 dark:text-gray-400">{{ label }}</span>
                  }
                  <ng-icon
                    name="heroChevronRight"
                    class="size-4 shrink-0 text-gray-400 dark:text-gray-500"
                    aria-hidden="true"
                  />
                </span>
              </button>

              <ng-template #effortMenu>
                <div
                  cdkMenu
                  class="w-56 rounded-md bg-white p-1.5 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
                  role="menu"
                  aria-orientation="vertical"
                >
                  <p class="px-3 py-2 text-xs/4 text-gray-500 dark:text-gray-400">
                    Higher effort means more thorough responses, but takes longer and costs more.
                  </p>
                  @for (level of effort.levels; track level) {
                    <button
                      cdkMenuItem
                      type="button"
                      (click)="selectEffort(level)"
                      class="flex w-full items-center justify-between gap-2 rounded-xs px-3 py-2 text-sm/5 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
                      role="menuitem"
                    >
                      <span class="flex items-center gap-1.5">
                        <span>{{ levelLabel(level) }}</span>
                        @if (level === effort.defaultLevel) {
                          <span
                            class="rounded-sm bg-gray-100 px-1 py-0.5 text-[10px]/3 font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-400"
                            >Default</span
                          >
                        }
                      </span>
                      @if (level === modelService.selectedEffort()) {
                        <ng-icon
                          name="heroCheck"
                          class="size-4 shrink-0 text-primary-500 dark:text-slate-400"
                          aria-hidden="true"
                        />
                      }
                    </button>
                  }
                </div>
              </ng-template>
            }

            @if (modelService.moreModels().length > 0) {
              <div class="my-1.5 border-t border-gray-200 dark:border-gray-700"></div>
              <button
                cdkMenuItem
                type="button"
                [cdkMenuTriggerFor]="moreModelsMenu"
                [cdkMenuPosition]="submenuPositions"
                class="flex w-full items-center justify-between gap-2 rounded-xs px-3 py-2 text-sm/5 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
                role="menuitem"
              >
                <span>More models</span>
                <ng-icon
                  name="heroChevronRight"
                  class="size-4 shrink-0 text-gray-400 dark:text-gray-500"
                  aria-hidden="true"
                />
              </button>

              <ng-template #moreModelsMenu>
                <div
                  cdkMenu
                  class="max-h-96 w-72 overflow-y-auto rounded-md bg-white p-1.5 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
                  role="menu"
                  aria-orientation="vertical"
                >
                  @for (model of modelService.moreModels(); track model.modelId) {
                    <app-model-option
                      cdkMenuItem
                      [model]="model"
                      [selected]="isSelected(model)"
                      [successorName]="modelService.modelNameFor(model.replacedBy)"
                      (cdkMenuItemTriggered)="selectModel(model)"
                    />
                  }
                </div>
              </ng-template>
            }
          }
        </div>
      </ng-template>

    </div>
  `,
  styles: `
@reference "../../../styles/theme.css";


@keyframes fade-in {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

@keyframes slide-in-from-top {
  from {
    transform: translateY(-0.25rem);
  }
  to {
    transform: translateY(0);
  }
}

.animate-in {
  animation: fade-in 200ms ease-out, slide-in-from-top 200ms ease-out;
}

.rotate-180 {
  transform: rotate(180deg);
}
  `
})
export class ModelDropdownComponent {
  // Inject services
  protected modelService = inject(ModelService);
  protected sessionService = inject(SessionService);
  private cacheGuard = inject(PromptCacheGuardService);

  /**
   * Whether the model menu is open, driving the trigger's chevron. Fed by the
   * trigger's `cdkMenuOpened` / `cdkMenuClosed` outputs — `CdkMenu` itself has
   * no `opened` output, so an `(opened)` binding on the panel never fires.
   */
  private readonly menuOpen = signal(false);

  /** The effort control for the selected model, or null when it offers none. */
  protected readonly effortControl = this.modelService.effortControl;

  /** Effort level to show in the trigger and the submenu row, already labelled. */
  protected readonly activeEffortLabel = computed<string | null>(() => {
    const level = this.modelService.selectedEffort();
    return level ? effortLevelLabel(level) : null;
  });

  /**
   * `compact` is the picker as it sits in the line beneath a compact composer:
   * the meta line's type size, and a menu that opens upward from the right,
   * because that line is the bottom-right corner of the viewport. `default` is
   * the picker in the empty state's control bar.
   */
  readonly size = input<'default' | 'compact'>('default');

  /**
   * Which way the compact picker's menu prefers to open. Up suits a thread's
   * composer pinned to the bottom of the viewport; down suits the empty
   * state's, which sits mid-page with the room beneath it. Either flips when
   * the preferred side is cramped.
   */
  readonly menuOpens = input<'up' | 'down'>('up');

  protected readonly triggerSizeClass = computed(() =>
    this.size() === 'compact' ? 'gap-1 px-2 py-1 text-xs/5' : 'gap-1.5 px-3 py-1.5 text-sm/5',
  );

  protected readonly lockedClass = computed(() =>
    this.size() === 'compact' ? 'gap-1.5 px-2 py-1 text-xs/5' : 'gap-2 px-3 py-1.5 text-sm/5',
  );

  // Menu positioning - below the trigger's left edge, flipping above when there
  // is no room; the compact picker anchors to its right edge, above or below
  // as `menuOpens` prefers.
  protected readonly menuPositions = computed<ConnectedPosition[]>(() => {
    if (this.size() === 'compact') {
      const above: ConnectedPosition = { originX: 'end', originY: 'top', overlayX: 'end', overlayY: 'bottom', offsetY: -8 };
      const below: ConnectedPosition = { originX: 'end', originY: 'bottom', overlayX: 'end', overlayY: 'top', offsetY: 8 };
      return this.menuOpens() === 'up' ? [above, below] : [below, above];
    }
    return [
      { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
      { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -8 },
    ];
  });

  // Submenus fly out to the right of their parent row, flipping to the left
  // when there isn't room — the picker sits in the composer, which can be
  // close to either edge depending on the sidebar state.
  protected submenuPositions: ConnectedPosition[] = [
    {
      originX: 'end',
      originY: 'top',
      overlayX: 'start',
      overlayY: 'top',
      offsetX: 4
    },
    {
      originX: 'start',
      originY: 'top',
      overlayX: 'end',
      overlayY: 'top',
      offsetX: -4
    },
    {
      originX: 'end',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'bottom',
      offsetX: 4
    },
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'end',
      overlayY: 'bottom',
      offsetX: -4
    }
  ];

  protected isMenuOpen(): boolean {
    return this.menuOpen();
  }

  protected onMenuOpened(): void {
    this.menuOpen.set(true);
  }

  protected onMenuClosed(): void {
    this.menuOpen.set(false);
  }

  protected levelLabel(level: string): string {
    return effortLevelLabel(level);
  }

  /**
   * Change the effort level. The level is part of the request the prompt
   * cache is keyed on, so in an open conversation the user is asked first
   * (when the cache is warm and the re-write is worth asking about).
   */
  protected async selectEffort(level: string): Promise<void> {
    const current = this.modelService.selectedEffort();
    if (level === current) return;
    if (current && this.sessionService.hasCurrentSession()) {
      const confirmed = await this.cacheGuard.confirm({
        kind: 'effort',
        model: this.modelService.selectedModel(),
        fromLabel: effortLevelLabel(current),
        toLabel: effortLevelLabel(level),
        toLevel: level,
      });
      if (!confirmed) return;
    }
    this.modelService.setEffort(level);
  }

  /**
   * Choose a model. An open conversation continues on the new model in place,
   * keeping its Agent or project binding. The next message re-processes the
   * whole conversation on the new model's cache, so the user is asked first
   * (when the cache is warm and the re-write is worth asking about).
   */
  protected async selectModel(model: ManagedModel): Promise<void> {
    const current = this.modelService.selectedModel();
    if (current.modelId === model.modelId) return;
    if (current.modelId && this.sessionService.hasCurrentSession()) {
      const confirmed = await this.cacheGuard.confirm({ kind: 'model', from: current, to: model });
      if (!confirmed) return;
    }
    this.modelService.setSelectedModel(model);
  }

  protected isSelected(model: ManagedModel): boolean {
    const selected = this.modelService.selectedModel();
    return selected?.modelId === model.modelId;
  }
}
