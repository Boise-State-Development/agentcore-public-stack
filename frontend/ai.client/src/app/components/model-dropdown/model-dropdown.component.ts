import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { Router } from '@angular/router';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck } from '@ng-icons/heroicons/outline';
import { ModelService } from '../../session/services/model/model.service';
import { SessionService } from '../../session/services/session/session.service';
import { ManagedModel } from '../../admin/manage-models/models/managed-model.model';

@Component({
  selector: 'app-model-dropdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenuTrigger, CdkMenu, CdkMenuItem, NgIcon],
  providers: [provideIcons({ heroCheck })],
  template: `
    <div class="relative">
      <button
        type="button"
        [cdkMenuTriggerFor]="modelMenu"
        [cdkMenuPosition]="menuPositions"
        class="flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-white/5 dark:hover:text-gray-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)]"
        aria-label="Select model"
      >
        <span>{{ modelService.selectedModel()?.modelName || 'Loading...' }}</span>
        <svg
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
          class="size-4 transition-transform"
          [class.rotate-180]="isMenuOpen()"
        >
          <path
            fill-rule="evenodd"
            d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
            clip-rule="evenodd"
          />
        </svg>
      </button>

      <ng-template #modelMenu>
        <div
          cdkMenu
          (closed)="onMenuClosed()"
          (opened)="onMenuOpened()"
          class="w-64 rounded-md bg-white shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10 animate-in fade-in slide-in-from-top-1 duration-200"
          role="menu"
          aria-orientation="vertical"
        >
          <div class="p-1">
            @if (modelService.modelsLoading()) {
              <div class="px-3 py-2 text-sm text-gray-500 dark:text-gray-400">
                Loading models...
              </div>
            } @else if (modelService.modelsError()) {
              <div class="px-3 py-2 text-sm text-red-600 dark:text-red-400">
                {{ modelService.modelsError() }}
              </div>
            } @else if (modelService.availableModels().length === 0) {
              <!-- Show default model option when no models are available -->
              <button
                cdkMenuItem
                type="button"
                class="flex w-full items-center justify-between px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                role="menuitem"
                disabled
              >
                <div class="flex flex-col items-start">
                  <span class="font-medium">{{ modelService.selectedModel()?.modelName || 'System Default' }}</span>
                  <span class="text-xs text-gray-500 dark:text-gray-400">Using backend default</span>
                </div>
                <ng-icon name="heroCheck" class="size-5 text-primary-500 dark:text-slate-400" aria-hidden="true" />
              </button>
            } @else {
              @for (model of modelService.availableModels(); track model.modelId) {
                <button
                  cdkMenuItem
                  type="button"
                  (click)="selectModel(model)"
                  class="flex w-full items-center justify-between px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                  role="menuitem"
                >
                  <div class="min-w-0 text-left">
                    <div class="truncate font-medium">{{ model.modelName }}</div>
                    <div class="truncate text-xs text-gray-500 dark:text-gray-400">{{ model.providerName }}</div>
                  </div>

                  @if (isSelected(model)) {
                    <ng-icon name="heroCheck" class="size-5 shrink-0 text-primary-500 dark:text-slate-400" aria-hidden="true" />
                  } @else if (sessionService.hasCurrentSession()) {
                    <span class="shrink-0 rounded-full border border-tertiary-500 px-2 py-0.5 text-xs font-medium text-tertiary-600 dark:border-tertiary-400 dark:text-tertiary-400">New chat</span>
                  }
                </button>
              }
            }
          </div>
        </div>
      </ng-template>
    </div>
  `,
  styles: `
@import "tailwindcss";

@custom-variant dark (&:where(.dark, .dark *));

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
  private router = inject(Router);

  // Internal state
  protected menuOpen = false;

  // Menu positioning - align to bottom-left of trigger
  protected menuPositions: ConnectedPosition[] = [
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'top',
      offsetY: 8
    },
    {
      originX: 'start',
      originY: 'top',
      overlayX: 'start',
      overlayY: 'bottom',
      offsetY: -8
    }
  ];

  protected isMenuOpen(): boolean {
    return this.menuOpen;
  }

  protected onMenuOpened(): void {
    this.menuOpen = true;
  }

  protected onMenuClosed(): void {
    this.menuOpen = false;
  }

  protected selectModel(model: ManagedModel): void {
    // If in an active session and selecting a different model, navigate to new chat
    if (this.sessionService.hasCurrentSession() && !this.isSelected(model)) {
      this.modelService.setSelectedModel(model);
      this.router.navigate(['']);
    } else {
      this.modelService.setSelectedModel(model);
    }
  }

  protected isSelected(model: ManagedModel): boolean {
    const selected = this.modelService.selectedModel();
    return selected?.modelId === model.modelId;
  }
}
