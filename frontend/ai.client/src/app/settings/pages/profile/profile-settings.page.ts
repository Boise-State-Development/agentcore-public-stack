import {
  Component,
  ChangeDetectionStrategy,
  inject,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroDocument, heroChevronRight } from '@ng-icons/heroicons/outline';
import { UserService } from '../../../auth/user.service';

@Component({
  selector: 'app-profile-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({ heroDocument, heroChevronRight }),
  ],
  host: { class: 'block' },
  template: `
    <div class="flex flex-col gap-8">
      <!-- Section header -->
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Profile</h2>
        <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
          Your personal information managed by your identity provider.
        </p>
      </div>

      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <!-- Avatar + name section -->
        <div class="flex items-center gap-6 p-6">
          @if (userService.currentUser()?.picture; as picture) {
            <img
              [src]="picture"
              alt="Profile photo"
              class="size-20 rounded-full object-cover ring-2 ring-gray-200 dark:ring-white/10"
            />
          } @else {
            <div class="flex size-20 items-center justify-center rounded-full bg-linear-to-br from-blue-500 to-indigo-600 text-2xl font-bold text-white ring-2 ring-gray-200 dark:ring-white/10">
              {{ getInitials() }}
            </div>
          }
          <div>
            <h3 class="text-base/7 font-semibold text-gray-900 dark:text-white">
              {{ userService.currentUser()?.fullName ?? 'Unknown User' }}
            </h3>
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">
              {{ userService.currentUser()?.email ?? '' }}
            </p>
            <span class="mt-1 inline-flex items-center rounded-xs bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
              Managed by Entra ID
            </span>
          </div>
        </div>

        <!-- Read-only details -->
        <div class="border-t border-gray-200 dark:border-white/10">
          <dl class="divide-y divide-gray-200 dark:divide-white/10">
            <div class="grid grid-cols-1 gap-1 px-6 py-4 sm:grid-cols-3 sm:items-center">
              <dt class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Full name</dt>
              <dd class="text-sm/6 text-gray-900 sm:col-span-2 dark:text-white">
                {{ userService.currentUser()?.fullName ?? '-' }}
              </dd>
            </div>
            <div class="grid grid-cols-1 gap-1 px-6 py-4 sm:grid-cols-3 sm:items-center">
              <dt class="text-sm/6 font-medium text-gray-500 dark:text-gray-400">Email</dt>
              <dd class="text-sm/6 text-gray-900 sm:col-span-2 dark:text-white">
                {{ userService.currentUser()?.email ?? '-' }}
              </dd>
            </div>
          </dl>
        </div>
      </div>

      <!-- My Files -->
      <div class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800">
        <a
          routerLink="/files"
          class="flex items-center justify-between gap-4 p-6 transition-colors hover:bg-gray-50 dark:hover:bg-white/5"
        >
          <div class="flex items-start gap-3">
            <div class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-gray-100 dark:bg-white/10">
              <ng-icon name="heroDocument" class="size-4 text-gray-500 dark:text-gray-400" />
            </div>
            <div>
              <span class="text-sm/6 font-medium text-gray-900 dark:text-white">My Files</span>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Browse and manage your uploaded files.
              </p>
            </div>
          </div>
          <ng-icon name="heroChevronRight" class="size-5 shrink-0 text-gray-400 dark:text-gray-500" />
        </a>
      </div>
    </div>
  `,
})
export class ProfileSettingsPage {
  readonly userService = inject(UserService);

  getInitials(): string {
    const user = this.userService.currentUser();
    if (!user) return '?';
    return `${user.firstName?.[0] ?? ''}${user.lastName?.[0] ?? ''}`.toUpperCase();
  }
}
