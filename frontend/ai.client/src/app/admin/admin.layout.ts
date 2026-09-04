import {
  Component,
  ChangeDetectionStrategy,
  computed,
  inject,
  OnInit,
  Signal,
} from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { AdminMarketplaceService } from './marketplace/services/admin-marketplace.service';
import { UserService } from '../auth/user.service';
import { AdminScopeId } from './admin-scope.model';
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
  heroClipboardDocumentList,
  heroBars3,
  heroSparkles,
  heroInbox,
  heroFlag,
  heroRectangleStack,
  heroStar,
  heroBuildingLibrary,
  heroTag,
  heroBookmark,
} from '@ng-icons/heroicons/outline';

interface NavItem {
  label: string;
  icon: string;
  route: string;
  /**
   * The admin scope that grants this entry. Must match the `data.scope` on the
   * corresponding route in `admin.routes.ts` — an entry linking somewhere the
   * scope guard will refuse is worse than no entry at all.
   */
  scope: AdminScopeId;
  /**
   * A count that badges this entry (D10). A signal rather than a number so the badge is
   * live — triaging the last report has to empty the badge without a reload, or the nav
   * starts lying about work that is already done.
   */
  badge?: Signal<number>;
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
      heroClipboardDocumentList,
      heroBars3,
      heroSparkles,
      heroInbox,
      heroFlag,
      heroRectangleStack,
      heroStar,
      heroBuildingLibrary,
      heroTag,
      heroBookmark,
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
          <aside class="lg:sticky lg:top-20 lg:max-h-[calc(100dvh-6rem)] lg:w-60 lg:shrink-0 lg:self-start lg:overflow-y-auto">
            <!-- Mobile dropdown (shown on small screens) -->
            <div class="lg:hidden">
              <label for="admin-nav" class="sr-only">Admin section</label>
              <select
                id="admin-nav"
                class="block w-full rounded-sm border-gray-300 bg-white py-2 pl-3 pr-10 text-base text-gray-900 focus:border-primary-500 focus:outline-hidden focus:ring-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                (change)="onMobileNavChange($event)"
              >
                @for (group of navGroups(); track group.label) {
                  <optgroup [label]="group.label">
                    @for (item of group.items; track item.route) {
                      <!-- No badge element on mobile: an <option> renders text only, so
                           the count goes inline or it is invisible here. -->
                      <option [value]="item.route">
                        {{ item.label }}{{ item.badge && item.badge() > 0 ? ' (' + item.badge() + ')' : '' }}
                      </option>
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
                            <span class="min-w-0 flex-1">{{ item.label }}</span>
                            @if (item.badge; as badge) {
                              @if (badge() > 0) {
                                <span
                                  class="inline-flex min-w-5 shrink-0 items-center justify-center rounded-full bg-primary-accessible px-1.5 text-xs/5 font-semibold text-white"
                                  [attr.aria-label]="badge() + ' waiting'"
                                >
                                  {{ badge() }}
                                </span>
                              }
                            }
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
export class AdminLayout implements OnInit {
  private router = inject(Router);
  private marketplace = inject(AdminMarketplaceService);
  private userService = inject(UserService);

  private readonly allNavGroups: NavGroup[] = [
    {
      label: 'Usage & Spend',
      items: [
        { label: 'Cost Analytics', icon: 'heroCurrencyDollar', route: '/admin/costs', scope: 'admin.costs' },
        { label: 'Quotas', icon: 'heroScale', route: '/admin/quota', scope: 'admin.quota' },
        { label: 'Fine-Tuning', icon: 'heroAcademicCap', route: '/admin/fine-tuning', scope: 'admin.fine_tuning' },
      ],
    },
    {
      label: 'AI Configuration',
      items: [
        { label: 'Models', icon: 'heroPencilSquare', route: '/admin/manage-models', scope: 'admin.models' },
        { label: 'Tools', icon: 'heroWrenchScrewdriver', route: '/admin/tools', scope: 'admin.tools' },
        { label: 'Skills', icon: 'heroSparkles', route: '/admin/skills', scope: 'admin.skills' },
        { label: 'Connectors', icon: 'heroLink', route: '/admin/connectors', scope: 'admin.connectors' },
      ],
    },
    {
      // Agent Marketplace. Six of D10's seven surfaces; Default Pins is the seventh and
      // is listed here even though its route lives under Roles, because the AppRole
      // record is the source of truth for a seed.
      //
      // Two entries carry counts (D10): work waiting should be *visible* rather than
      // discovered by clicking into a queue to see whether it is empty.
      label: 'Agent Marketplace',
      items: [
        {
          label: 'Review Queue',
          icon: 'heroInbox',
          route: '/admin/marketplace/review',
          scope: 'admin.marketplace',
          badge: this.marketplace.pendingCount,
        },
        {
          label: 'Reports',
          icon: 'heroFlag',
          route: '/admin/marketplace/reports',
          scope: 'admin.marketplace',
          badge: this.marketplace.openReportCount,
        },
        { label: 'Listings', icon: 'heroRectangleStack', route: '/admin/marketplace/listings', scope: 'admin.marketplace' },
        { label: 'Store Front', icon: 'heroStar', route: '/admin/marketplace/store-front', scope: 'admin.marketplace' },
        { label: 'Categories', icon: 'heroTag', route: '/admin/marketplace/categories', scope: 'admin.marketplace' },
        { label: 'Publishers', icon: 'heroBuildingLibrary', route: '/admin/marketplace/publishers', scope: 'admin.marketplace' },
        { label: 'Default Pins', icon: 'heroBookmark', route: '/admin/marketplace/default-pins', scope: 'admin.marketplace' },
      ],
    },
    {
      label: 'Identity & Access',
      items: [
        { label: 'Users', icon: 'heroUsers', route: '/admin/users', scope: 'admin.users' },
        { label: 'Roles', icon: 'heroKey', route: '/admin/roles', scope: 'admin.roles' },
        { label: 'Auth Providers', icon: 'heroFingerPrint', route: '/admin/auth-providers', scope: 'admin.auth_providers' },
        { label: 'Audit Log', icon: 'heroClipboardDocumentList', route: '/admin/audit', scope: 'admin.audit' },
      ],
    },
    {
      label: 'Customization',
      items: [
        { label: 'User Menu Links', icon: 'heroBars3', route: '/admin/manage-user-menu-links', scope: 'admin.user_menu_links' },
        { label: 'Conversation Modes', icon: 'heroSparkles', route: '/admin/system-prompts', scope: 'admin.system_prompts' },
      ],
    },
  ];

  /**
   * Nav groups the current admin can actually open.
   *
   * Filtered by delegated admin scope: `system_admin` sees everything (
   * `hasAdminScope` short-circuits for it), so this is a no-op for a full
   * admin. A group whose items are all filtered out is dropped entirely rather
   * than left as an empty heading.
   *
   * Linking to a page the scope guard would bounce is worse than not linking
   * it, which is why `NavItem.scope` is required rather than optional.
   *
   * Note this is presentation only. The guard is the client-side gate and the
   * server is the actual boundary; hiding the link just means a delegated
   * admin never clicks into a bounce.
   */
  readonly navGroups = computed<NavGroup[]>(() =>
    this.allNavGroups
      .map(group => ({
        ...group,
        items: group.items.filter(item => this.userService.hasAdminScope(item.scope)),
      }))
      .filter(group => group.items.length > 0)
  );

  /**
   * One small call for both badges, on every admin page.
   *
   * The counts have to be right wherever the admin is standing, not only once they open
   * a queue — that is what makes the badge a prompt rather than a confirmation. It is a
   * dedicated endpoint rather than two queue loads so this does not put a table scan and
   * a full row projection behind every click in the console, and it swallows failures:
   * a badge is orientation, and an unreachable count must not break the shell.
   */
  ngOnInit(): void {
    // Gated on the scope: the counts endpoint is guarded by `admin.marketplace`,
    // so firing it unconditionally would mean a guaranteed 403 on every single
    // navigation for an admin who does not hold that scope — a console full of
    // console-errors for a badge they cannot see anyway.
    if (this.userService.hasAdminScope('admin.marketplace')) {
      void this.marketplace.refreshQueueCounts();
    }
  }

  onMobileNavChange(event: Event): void {
    const select = event.target as HTMLSelectElement;
    this.router.navigateByUrl(select.value);
  }
}
