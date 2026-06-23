import {
  Component,
  ChangeDetectionStrategy,
  computed,
  inject,
} from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { ChatModeService } from '../services/chat-mode/chat-mode.service';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroShieldCheck,
  heroCurrencyDollar,
  heroScale,
  heroAcademicCap,
  heroPencilSquare,
  heroWrenchScrewdriver,
  heroLink,
  heroUsers,
  heroKey,
  heroFingerPrint,
  heroBars3,
  heroSparkles,
} from '@ng-icons/heroicons/outline';

interface NavItem {
  label: string;
  icon: string;
  route: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

@Component({
  selector: 'app-admin-layout',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive, RouterOutlet, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroShieldCheck,
      heroCurrencyDollar,
      heroScale,
      heroAcademicCap,
      heroPencilSquare,
      heroWrenchScrewdriver,
      heroLink,
      heroUsers,
      heroKey,
      heroFingerPrint,
      heroBars3,
      heroSparkles,
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
            <ng-icon name="heroShieldCheck" class="size-5 text-gray-400 dark:text-gray-500" />
            <h1 class="text-base/7 font-semibold text-gray-900 dark:text-white">Admin</h1>
          </div>
        </div>
      </div>

      <div class="mx-auto max-w-[96rem] px-4 py-8 sm:px-6 lg:px-8">
        <div class="lg:flex lg:gap-x-8">
          <!-- Sidebar Navigation -->
          <aside class="lg:w-60 lg:shrink-0">
            <!-- Mobile dropdown (shown on small screens) -->
            <div class="lg:hidden">
              <label for="admin-nav" class="sr-only">Admin section</label>
              <select
                id="admin-nav"
                class="block w-full rounded-sm border-gray-300 bg-white py-2 pl-3 pr-10 text-base text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-blue-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                (change)="onMobileNavChange($event)"
              >
                @for (group of navGroups(); track group.label) {
                  <optgroup [label]="group.label">
                    @for (item of group.items; track item.route) {
                      <option [value]="item.route">{{ item.label }}</option>
                    }
                  </optgroup>
                }
              </select>
            </div>

            <!-- Desktop sidebar -->
            <nav class="hidden lg:block" aria-label="Admin navigation">
              <div class="flex flex-col gap-6">
                @for (group of navGroups(); track group.label) {
                  <div>
                    <h2 class="px-3 text-xs/5 font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                      {{ group.label }}
                    </h2>
                    <ul role="list" class="mt-2 flex flex-col gap-1">
                      @for (item of group.items; track item.route) {
                        <li>
                          <a
                            [routerLink]="item.route"
                            routerLinkActive="bg-gray-100 text-gray-900 dark:bg-white/10 dark:text-white"
                            class="group flex items-center gap-x-3 whitespace-nowrap rounded-md px-3 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white"
                          >
                            <ng-icon [name]="item.icon" class="size-5 shrink-0 text-gray-400 group-hover:text-gray-500 dark:text-gray-500 dark:group-hover:text-gray-300" />
                            {{ item.label }}
                          </a>
                        </li>
                      }
                    </ul>
                  </div>
                }
              </div>
            </nav>
          </aside>

          <!-- Content area -->
          <main class="mt-8 min-w-0 lg:mt-0 lg:flex-1">
            <router-outlet />
          </main>
        </div>
      </div>
    </div>
  `,
})
export class AdminLayout {
  private router = inject(Router);
  private chatMode = inject(ChatModeService);

  private readonly allNavGroups: NavGroup[] = [
    {
      label: 'Usage & Spend',
      items: [
        { label: 'Cost Analytics', icon: 'heroCurrencyDollar', route: '/admin/costs' },
        { label: 'Quotas', icon: 'heroScale', route: '/admin/quota' },
        { label: 'Fine-Tuning', icon: 'heroAcademicCap', route: '/admin/fine-tuning' },
      ],
    },
    {
      label: 'AI Configuration',
      items: [
        { label: 'Models', icon: 'heroPencilSquare', route: '/admin/manage-models' },
        { label: 'Tools', icon: 'heroWrenchScrewdriver', route: '/admin/tools' },
        { label: 'Skills', icon: 'heroSparkles', route: '/admin/skills' },
        { label: 'Connectors', icon: 'heroLink', route: '/admin/connectors' },
      ],
    },
    {
      label: 'Identity & Access',
      items: [
        { label: 'Users', icon: 'heroUsers', route: '/admin/users' },
        { label: 'Roles', icon: 'heroKey', route: '/admin/roles' },
        { label: 'Auth Providers', icon: 'heroFingerPrint', route: '/admin/auth-providers' },
      ],
    },
    {
      label: 'Customization',
      items: [
        { label: 'User Menu Links', icon: 'heroBars3', route: '/admin/manage-user-menu-links' },
        { label: 'Conversation Modes', icon: 'heroSparkles', route: '/admin/system-prompts' },
      ],
    },
  ];

  /**
   * Nav groups with the Skills entry hidden while the skills feature is
   * disabled for this environment (deferred release). The pages stay routed
   * but unlinked; the backend forces tools mode and 404s the skills APIs.
   */
  readonly navGroups = computed<NavGroup[]>(() => {
    if (this.chatMode.skillsEnabled()) return this.allNavGroups;
    return this.allNavGroups.map((group) => ({
      ...group,
      items: group.items.filter((item) => item.route !== '/admin/skills'),
    }));
  });

  onMobileNavChange(event: Event): void {
    const select = event.target as HTMLSelectElement;
    this.router.navigateByUrl(select.value);
  }
}
