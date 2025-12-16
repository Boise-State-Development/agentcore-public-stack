// user-dropdown.component.ts
import { Component, input, output, ChangeDetectionStrategy } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroChevronDown,
  heroCpuChip,
  heroPencilSquare,
  heroCurrencyDollar,
  heroArrowRightOnRectangle
} from '@ng-icons/heroicons/outline';

export interface User {
  name: string;
  email?: string;
  picture?: string;
}

@Component({
  selector: 'app-user-dropdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, CdkMenuTrigger, CdkMenu, CdkMenuItem, NgIcon],
  providers: [
    provideIcons({
      heroChevronDown,
      heroCpuChip,
      heroPencilSquare,
      heroCurrencyDollar,
      heroArrowRightOnRectangle
    })
  ],
  template: `
    <div class="relative">
      <button
        type="button"
        [cdkMenuTriggerFor]="userMenu"
        [cdkMenuPosition]="menuPositions"
        class="relative flex items-center rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)]"
        aria-label="User menu"
      >
        <span class="absolute -inset-1.5"></span>
        <span class="sr-only">Open user menu</span>
        
        @if (user().picture) {
          <img 
            [src]="user().picture" 
            [alt]="user().name" 
            class="size-8 rounded-full bg-gray-50 outline -outline-offset-1 outline-black/5 dark:bg-gray-800 dark:outline-white/10" 
          />
        } @else {
          <div class="size-8 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center outline -outline-offset-1 outline-black/5 dark:outline-white/10">
            <span class="text-xs font-semibold text-gray-600 dark:text-gray-300">
              {{ getUserInitial() }}
            </span>
          </div>
        }
        
        <span class="hidden lg:flex lg:items-center">
          <span aria-hidden="true" class="ml-4 text-sm/6 font-semibold text-gray-900 dark:text-white">
            {{ user().name }}
          </span>
          <ng-icon
            name="heroChevronDown"
            class="ml-2 size-5 text-gray-400 transition-transform"
            [class.rotate-180]="isMenuOpen()"
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
            <!-- <div class="px-3 py-2 border-b border-gray-200 dark:border-gray-700">
              <p class="text-sm/5 font-semibold text-gray-900 dark:text-white">
                {{ user().name }}
              </p>
              @if (user().email) {
                <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                  {{ user().email }}
                </p>
              }
            </div> -->

            <!-- Menu items -->
            <div class="py-1">
              <!-- Admin-only menu items -->
              @if (isAdmin()) {
                <a
                  cdkMenuItem
                  routerLink="/admin/bedrock/models"
                  class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                  role="menuitem"
                >
                  <ng-icon
                    name="heroCpuChip"
                    class="size-5 text-gray-400 dark:text-gray-500"
                  />
                  <span>Bedrock Models</span>
                </a>

                <a
                  cdkMenuItem
                  routerLink="/admin/gemini/models"
                  class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                  role="menuitem"
                >
                  <ng-icon
                    name="heroCpuChip"
                    class="size-5 text-gray-400 dark:text-gray-500"
                  />
                  <span>Gemini Models</span>
                </a>

                <a
                  cdkMenuItem
                  routerLink="/admin/manage-models"
                  class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                  role="menuitem"
                >
                  <ng-icon
                    name="heroPencilSquare"
                    class="size-5 text-gray-400 dark:text-gray-500"
                  />
                  <span>Manage Models</span>
                </a>
              }

              <!-- Cost Dashboard (available to all users) -->
              <a
                cdkMenuItem
                routerLink="/costs"
                class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                role="menuitem"
              >
                <ng-icon
                  name="heroCurrencyDollar"
                  class="size-5 text-gray-400 dark:text-gray-500"
                />
                <span>Cost Dashboard</span>
              </a>

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
  // Inputs
  user = input.required<User>();
  isAdmin = input.required<boolean>();

  // Outputs
  logout = output<void>();
  
  // Internal state
  protected menuOpen = false;
  
  // Menu positioning - align to bottom-right of trigger
  protected menuPositions: ConnectedPosition[] = [
    {
      originX: 'end',
      originY: 'bottom',
      overlayX: 'end',
      overlayY: 'top',
      offsetY: 8
    },
    {
      originX: 'end',
      originY: 'top',
      overlayX: 'end',
      overlayY: 'bottom',
      offsetY: -8
    }
  ];
  
  protected isMenuOpen(): boolean {
    return this.menuOpen;
  }
  
  protected getUserInitial(): string {
    return this.user().name.charAt(0).toUpperCase();
  }
  
  protected onMenuOpened(): void {
    this.menuOpen = true;
  }
  
  protected onMenuClosed(): void {
    this.menuOpen = false;
  }
  
  protected handleLogout(): void {
    console.log('Logout clicked for user:', this.user().name);
    this.logout.emit();
  }
}