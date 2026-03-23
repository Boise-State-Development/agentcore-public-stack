// user-dropdown.component.ts
import { Component, input, output, ChangeDetectionStrategy, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroChevronUpDown,
  heroArrowRightOnRectangle,
  heroCommandLine,
  heroSparkles,
  heroSun,
  heroMoon,
  heroComputerDesktop,
  heroChatBubbleLeftRight,
  heroDocument,
  heroBriefcase,
  heroCog6Tooth,
} from '@ng-icons/heroicons/outline';
import { ThemeService, ThemePreference } from './theme-toggle/theme.service';
import { ConfigService } from '../../../services/config.service';

export interface User {
  firstName: string;
  lastName: string;
  fullName: string;
  email?: string;
  picture?: string;
}

@Component({
  selector: 'app-user-dropdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, CdkMenuTrigger, CdkMenu, CdkMenuItem, NgIcon],
  providers: [
    provideIcons({
      heroChevronUpDown,
      heroArrowRightOnRectangle,
      heroCommandLine,
      heroSparkles,
      heroSun,
      heroBriefcase,
      heroMoon,
      heroComputerDesktop,
      heroChatBubbleLeftRight,
      heroDocument,
      heroCog6Tooth,
    })
  ],
  template: `
    <div class="relative">
      <button
        type="button"
        [cdkMenuTriggerFor]="userMenu"
        [cdkMenuPosition]="menuPositionsComputed()"
        class="relative flex w-full items-center gap-3 rounded-lg px-2 py-2 hover:bg-gray-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)] dark:hover:bg-white/10"
        aria-label="User menu"
      >
        <span class="sr-only">Open user menu</span>

        @if (user().picture) {
          <img
            [src]="user().picture"
            [alt]="user().fullName"
            class="size-8 shrink-0 rounded-full bg-gray-50 outline -outline-offset-1 outline-black/5 dark:bg-gray-800 dark:outline-white/10"
          />
        } @else {
          <div class="size-8 shrink-0 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center outline -outline-offset-1 outline-black/5 dark:outline-white/10">
            <span class="text-xs font-semibold text-gray-600 dark:text-gray-300">
              {{ getUserInitial() }}
            </span>
          </div>
        }

        <span class="flex min-w-0 flex-1 items-center justify-between">
          <span class="truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
            {{ user().fullName }}
          </span>
          <ng-icon
            name="heroChevronUpDown"
            class="size-5 shrink-0 text-gray-400"
          />
        </span>
      </button>

      <ng-template #userMenu>
        <div
          cdkMenu
          (closed)="onMenuClosed()"
          (opened)="onMenuOpened()"
          class="w-56 rounded-md bg-white shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10 animate-in fade-in slide-in-from-top-1 duration-200"
          role="menu"
          aria-orientation="vertical"
        >
          <div class="p-1">
            <!-- User info section -->
            <div class="border-b border-gray-200 px-3 py-2 dark:border-gray-700">
              @if (user().email) {
                <p class="truncate text-xs/5 lowercase text-gray-500 dark:text-gray-400">
                  {{ user().email }}
                </p>
              }
            </div>

            <!-- Menu items -->
            <div class="py-1">
              <!-- Admin Dashboard (admin only) -->
              @if (isAdmin()) {
                <a
                  cdkMenuItem
                  routerLink="/admin"
                  class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                  role="menuitem"
                >
                  <ng-icon
                    name="heroCommandLine"
                    class="size-5 text-gray-400 dark:text-gray-500"
                  />
                  <span>Admin Dashboard</span>
                </a>
              }

              <!-- Settings -->
              <a
                cdkMenuItem
                routerLink="/settings"
                class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                role="menuitem"
              >
                <ng-icon
                  name="heroCog6Tooth"
                  class="size-5 text-gray-400 dark:text-gray-500"
                />
                <span>Settings</span>
              </a>
            </div>

            <!-- Logout section -->
            <div class="border-t border-gray-200 py-1 dark:border-gray-700">
              <button
                cdkMenuItem
                type="button"
                (click)="handleLogout()"
                class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                role="menuitem"
              >
                <ng-icon
                  name="heroArrowRightOnRectangle"
                  class="size-5 text-gray-400 dark:text-gray-500"
                />
                <span>Logout</span>
              </button>
            </div>

            <!-- Version -->
            @if (displayVersion(); as version) {
              <div class="border-t border-gray-200 px-3 py-2 dark:border-gray-700">
                <span class="text-xs text-gray-400 dark:text-gray-500">{{ version }}</span>
              </div>
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
export class UserDropdownComponent {
  private readonly themeService = inject(ThemeService);
  private readonly configService = inject(ConfigService);

  // Inputs
  user = input.required<User>();
  isAdmin = input.required<boolean>();

  // Outputs
  logout = output<void>();

  // Internal state
  protected menuOpen = false;

  // Theme state
  protected readonly currentPreference = this.themeService.preference;
  protected readonly currentTheme = this.themeService.theme;

  // Version
  protected readonly displayVersion = computed(() => {
    const version = this.configService.version();
    if (!version || version === 'unknown') return '';
    return version === 'dev' ? 'local' : `v${version}`;
  });

  // Menu positioning - opens upward (for sidenav bottom placement)
  private readonly sidenavPositions: ConnectedPosition[] = [
    {
      originX: 'start',
      originY: 'top',
      overlayX: 'start',
      overlayY: 'bottom',
      offsetY: -8
    },
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'top',
      offsetY: 8
    }
  ];

  // Computed signal for menu positions
  protected menuPositionsComputed = computed(() => this.sidenavPositions);

  protected isMenuOpen(): boolean {
    return this.menuOpen;
  }

  protected getUserInitial(): string {
    return this.user().fullName.charAt(0).toUpperCase();
  }

  protected onMenuOpened(): void {
    this.menuOpen = true;
  }

  protected onMenuClosed(): void {
    this.menuOpen = false;
  }

  protected selectTheme(preference: ThemePreference): void {
    this.themeService.setPreference(preference);
  }

  protected handleLogout(): void {
    this.logout.emit();
  }
}