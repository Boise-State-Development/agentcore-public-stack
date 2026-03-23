import {
  Component,
  ChangeDetectionStrategy,
  inject,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroSun,
  heroMoon,
  heroComputerDesktop,
  heroCheck,
} from '@ng-icons/heroicons/outline';
import { ThemeService, ThemePreference } from '../../../components/topnav/components/theme-toggle/theme.service';

@Component({
  selector: 'app-appearance-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroSun, heroMoon, heroComputerDesktop, heroCheck }),
  ],
  host: { class: 'block' },
  template: `
    <div class="flex flex-col gap-8">
      <!-- Section header -->
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Appearance</h2>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Customize how the application looks and feels.
        </p>
      </div>

      <!-- Theme selection -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <div class="p-6">
          <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">Theme</h3>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">Choose your preferred color scheme.</p>

          <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            @for (option of themeOptions; track option.value) {
              <button
                type="button"
                (click)="setTheme(option.value)"
                [class]="themeService.preference() === option.value
                  ? 'ring-2 ring-blue-600 dark:ring-blue-500'
                  : 'ring-1 ring-gray-200 dark:ring-white/10 hover:ring-gray-300 dark:hover:ring-white/20'"
                class="relative flex flex-col items-center gap-3 rounded-lg p-4 transition-all"
              >
                <!-- Preview swatch -->
                <div [class]="option.previewClasses" class="flex h-20 w-full items-end justify-center overflow-hidden rounded-md p-2">
                  <div class="flex w-full gap-1.5">
                    <div [class]="option.sidebarClasses" class="h-10 w-6 rounded-xs"></div>
                    <div class="flex flex-1 flex-col gap-1">
                      <div [class]="option.lineClasses" class="h-2 w-3/4 rounded-xs"></div>
                      <div [class]="option.lineClasses" class="h-2 w-1/2 rounded-xs"></div>
                      <div [class]="option.lineClasses" class="h-2 w-2/3 rounded-xs"></div>
                    </div>
                  </div>
                </div>

                <div class="flex items-center gap-2">
                  <ng-icon [name]="option.icon" class="size-4 text-gray-500 dark:text-gray-400" />
                  <span class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ option.label }}</span>
                </div>

                @if (themeService.preference() === option.value) {
                  <div class="absolute right-2 top-2 flex size-5 items-center justify-center rounded-full bg-blue-600 dark:bg-blue-500">
                    <ng-icon name="heroCheck" class="size-3 text-white" />
                  </div>
                }
              </button>
            }
          </div>
        </div>
      </div>

    </div>
  `,
})
export class AppearanceSettingsPage {
  readonly themeService = inject(ThemeService);

  readonly themeOptions = [
    {
      value: 'light' as ThemePreference,
      label: 'Light',
      icon: 'heroSun',
      previewClasses: 'bg-white border border-gray-200',
      sidebarClasses: 'bg-gray-100',
      lineClasses: 'bg-gray-200',
    },
    {
      value: 'dark' as ThemePreference,
      label: 'Dark',
      icon: 'heroMoon',
      previewClasses: 'bg-gray-900 border border-gray-700',
      sidebarClasses: 'bg-gray-800',
      lineClasses: 'bg-gray-700',
    },
    {
      value: 'system' as ThemePreference,
      label: 'System',
      icon: 'heroComputerDesktop',
      previewClasses: 'bg-linear-to-r from-white to-gray-900 border border-gray-300',
      sidebarClasses: 'bg-linear-to-b from-gray-100 to-gray-800',
      lineClasses: 'bg-linear-to-r from-gray-200 to-gray-700',
    },
  ];

  setTheme(value: ThemePreference): void {
    this.themeService.setPreference(value);
  }
}
