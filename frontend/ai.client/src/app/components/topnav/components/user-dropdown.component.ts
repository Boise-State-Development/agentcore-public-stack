// user-dropdown.component.ts
import { Component, input, output, ChangeDetectionStrategy } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';

export interface User {
  name: string;
  email?: string;
  picture?: string;
}

@Component({
  selector: 'app-user-dropdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, CdkMenuTrigger, CdkMenu, CdkMenuItem],
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
          <svg 
            viewBox="0 0 20 20" 
            fill="currentColor" 
            aria-hidden="true" 
            class="ml-2 size-5 text-gray-400 transition-transform"
            [class.rotate-180]="isMenuOpen()"
          >
            <path 
              d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z" 
              clip-rule="evenodd" 
              fill-rule="evenodd" 
            />
          </svg>
        </span>
      </button>

      <ng-template #userMenu>
        <div
          cdkMenu
          (closed)="onMenuClosed()"
          (opened)="onMenuOpened()"
          class="w-56 rounded-sm bg-white shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10 animate-in fade-in slide-in-from-top-1 duration-200"
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
                  <svg
                    class="size-5 text-gray-400 dark:text-gray-500"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    aria-hidden="true"
                  >
                    <path
                      d="M10 3.5a1.5 1.5 0 0 1 3 0V4a1 1 0 0 0 1 1h3a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-.5a1.5 1.5 0 0 0 0 3h.5a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-.5a1.5 1.5 0 0 0-3 0v.5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-3a1 1 0 0 0-1-1h-.5a1.5 1.5 0 0 1 0-3H4a1 1 0 0 0 1-1V6a1 1 0 0 1 1-1h3a1 1 0 0 0 1-1v-.5Z"
                    />
                  </svg>
                  <span>Bedrock Models</span>
                </a>

                <a
                  cdkMenuItem
                  routerLink="/admin/manage-models"
                  class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                  role="menuitem"
                >
                  <svg
                    class="size-5 text-gray-400 dark:text-gray-500"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    aria-hidden="true"
                  >
                    <path
                      d="M2.695 14.763l-1.262 3.154a.5.5 0 00.65.65l3.155-1.262a4 4 0 001.343-.885L17.5 5.5a2.121 2.121 0 00-3-3L3.58 13.42a4 4 0 00-.885 1.343z"
                    />
                  </svg>
                  <span>Manage Models</span>
                </a>
              }

              <button
                cdkMenuItem
                type="button"
                (click)="handleLogout()"
                class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700 rounded-xs outline-hidden"
                role="menuitem"
              >
                <svg
                  class="size-5 text-gray-400 dark:text-gray-500"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    fill-rule="evenodd"
                    d="M3 4.25A2.25 2.25 0 0 1 5.25 2h5.5A2.25 2.25 0 0 1 13 4.25v2a.75.75 0 0 1-1.5 0v-2a.75.75 0 0 0-.75-.75h-5.5a.75.75 0 0 0-.75.75v11.5c0 .414.336.75.75.75h5.5a.75.75 0 0 0 .75-.75v-2a.75.75 0 0 1 1.5 0v2A2.25 2.25 0 0 1 10.75 18h-5.5A2.25 2.25 0 0 1 3 15.75V4.25Z"
                    clip-rule="evenodd"
                  />
                  <path
                    fill-rule="evenodd"
                    d="M6 10a.75.75 0 0 1 .75-.75h9.546l-1.048-.943a.75.75 0 1 1 1.004-1.114l2.5 2.25a.75.75 0 0 1 0 1.114l-2.5 2.25a.75.75 0 1 1-1.004-1.114l1.048-.943H6.75A.75.75 0 0 1 6 10Z"
                    clip-rule="evenodd"
                  />
                </svg>
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