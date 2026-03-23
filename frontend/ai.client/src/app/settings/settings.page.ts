import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroUser,
  heroPaintBrush,
  heroChatBubbleLeftRight,
  heroLink,
  heroKey,
  heroChartBar,
  heroCog6Tooth,
} from '@ng-icons/heroicons/outline';

interface NavItem {
  label: string;
  icon: string;
  route: string;
  description: string;
}

@Component({
  selector: 'app-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive, RouterOutlet, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroUser,
      heroPaintBrush,
      heroChatBubbleLeftRight,
      heroLink,
      heroKey,
      heroChartBar,
      heroCog6Tooth,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div class="min-h-dvh">
      <!-- Top bar -->
      <div class="sticky top-0 z-10 border-b border-gray-200 bg-gray-50/80 backdrop-blur-sm dark:border-white/10 dark:bg-gray-900/50">
        <div class="flex h-14 items-center gap-4 px-4 sm:px-6 lg:px-8">
          <a
            routerLink="/"
            class="flex items-center gap-2 text-sm/6 font-medium text-gray-500 transition-colors hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
          >
            <ng-icon name="heroArrowLeft" class="size-4" />
            <span class="hidden sm:inline">Back to Chat</span>
          </a>
          <div class="h-5 w-px bg-gray-200 dark:bg-white/10"></div>
          <div class="flex items-center gap-2">
            <ng-icon name="heroCog6Tooth" class="size-5 text-gray-400 dark:text-gray-500" />
            <h1 class="text-base/7 font-semibold text-gray-900 dark:text-white">Settings</h1>
          </div>
        </div>
      </div>

      <div class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="lg:grid lg:grid-cols-12 lg:gap-x-8">
          <!-- Sidebar Navigation -->
          <aside class="lg:col-span-2">
            <!-- Mobile dropdown (shown on small screens) -->
            <div class="lg:hidden">
              <label for="settings-nav" class="sr-only">Settings section</label>
              <select
                id="settings-nav"
                class="block w-full rounded-sm border-gray-300 bg-white py-2 pl-3 pr-10 text-base text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-blue-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                (change)="onMobileNavChange($event)"
              >
                @for (item of navItems; track item.route) {
                  <option [value]="item.route">{{ item.label }}</option>
                }
              </select>
            </div>

            <!-- Desktop sidebar -->
            <nav class="hidden lg:block" aria-label="Settings navigation">
              <ul role="list" class="flex flex-col gap-1">
                @for (item of navItems; track item.route) {
                  <li>
                    <a
                      [routerLink]="item.route"
                      routerLinkActive="bg-gray-100 text-gray-900 dark:bg-white/10 dark:text-white"
                      [routerLinkActiveOptions]="{ exact: true }"
                      class="group flex items-center gap-x-3 rounded-md px-3 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white"
                    >
                      <ng-icon [name]="item.icon" class="size-5 shrink-0 text-gray-400 group-hover:text-gray-500 dark:text-gray-500 dark:group-hover:text-gray-300" />
                      {{ item.label }}
                    </a>
                  </li>
                }
              </ul>
            </nav>
          </aside>

          <!-- Content area -->
          <main class="mt-8 lg:col-span-10 lg:mt-0">
            <router-outlet />
          </main>
        </div>
      </div>
    </div>
  `,
})
export class SettingsPage {
  private router = inject(Router);

  readonly navItems: NavItem[] = [
    { label: 'Profile', icon: 'heroUser', route: '/settings/profile', description: 'Your personal information' },
    { label: 'Appearance', icon: 'heroPaintBrush', route: '/settings/appearance', description: 'Theme and display' },
    { label: 'Chat', icon: 'heroChatBubbleLeftRight', route: '/settings/chat', description: 'Chat preferences' },
    { label: 'Connections', icon: 'heroLink', route: '/settings/connections', description: 'Connected apps' },
    { label: 'API Keys', icon: 'heroKey', route: '/settings/api-keys', description: 'API key management' },
    { label: 'Usage', icon: 'heroChartBar', route: '/settings/usage', description: 'Usage and billing' },
  ];

  onMobileNavChange(event: Event): void {
    const select = event.target as HTMLSelectElement;
    this.router.navigateByUrl(select.value);
  }
}
