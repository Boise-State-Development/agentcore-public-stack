import {
  Component,
  ChangeDetectionStrategy,
  computed,
  inject,
  OnInit,
  Signal,
} from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { AdminMarketplaceService } from './marketplace/services/admin-marketplace.service';
import { UserService } from '../auth/user.service';
import { SidenavService } from '../services/sidenav/sidenav.service';
import { AdminScopeId } from './admin-scope.model';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCurrencyDollar,
  heroHandThumbDown,
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
  heroMegaphone,
  heroSparkles,
  heroInbox,
  heroFlag,
  heroRectangleStack,
  heroStar,
  heroBuildingLibrary,
  heroTag,
  heroBookmark,
  heroSquares2x2,
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

/**
 * The admin console's navigation, rendered as the *body of the app sidenav*
 * rather than as a second column inside the admin page.
 *
 * It used to be an `<aside>` in `AdminLayout`, which meant an admin page was
 * squeezed between two navigations — the chat sidenav on the left (listing
 * conversations they cannot reach from here) and this one — inside a
 * `max-w-7xl` box sized for prose. The console's widest surfaces (cost tables,
 * the model catalog, the role matrix) were the ones that suffered most.
 *
 * Swapping the sidenav's body instead gives those pages the full content area
 * and costs the admin nothing: `Sidenav` keeps its own frame (logo, collapse
 * control, user menu), so only the middle changes, and "Back to Chat" at the
 * top is the way out.
 */
@Component({
  selector: 'app-admin-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroCurrencyDollar,
      heroHandThumbDown,
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
      heroMegaphone,
      heroSparkles,
      heroInbox,
      heroFlag,
      heroRectangleStack,
      heroStar,
      heroBuildingLibrary,
      heroTag,
      heroBookmark,
      heroSquares2x2,
    }),
  ],
  host: { class: 'flex min-h-0 flex-1 flex-col' },
  template: `
    <div class="sidenav-body-enter shrink-0 px-3 pb-2">
      <a
        routerLink="/"
        (click)="sidenavService.close()"
        class="group flex w-full cursor-pointer items-center gap-3 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-gray-200/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500 dark:hover:bg-white/5"
      >
        <div class="flex size-7 shrink-0 items-center justify-center text-gray-500 dark:text-gray-400">
          <ng-icon name="heroArrowLeft" class="size-5" />
        </div>
        <span class="text-sm font-medium text-gray-700 dark:text-gray-300">Back to Chat</span>
      </a>
    </div>

    <nav class="flex-1 overflow-y-auto px-3 pb-4" aria-label="Admin navigation">
      <div class="flex flex-col gap-5">
        @for (group of navGroups(); track group.label; let i = $index) {
          <!-- The delay is a style binding rather than a class per index: the
               group count is whatever the admin's scopes leave behind, so a
               fixed ladder of .delay-1..n classes would be a lie the moment a
               delegated admin sees three groups instead of five. The motion
               itself is shared with the chat nav — see .sidenav-body-enter. -->
          <div class="sidenav-body-enter" [style.animation-delay.ms]="60 + i * 40">
            <h2 class="px-2 text-xs/5 font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              {{ group.label }}
            </h2>
            <ul role="list" class="mt-1 flex flex-col gap-0.5">
              @for (item of group.items; track item.route) {
                <li>
                  <a
                    [routerLink]="item.route"
                    routerLinkActive="bg-gray-200/80 text-gray-900 dark:bg-white/10 dark:text-white"
                    (click)="sidenavService.close()"
                    class="group flex items-center gap-3 rounded-md px-2 py-1.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-200/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500 dark:text-gray-300 dark:hover:bg-white/5"
                  >
                    <div class="flex size-7 shrink-0 items-center justify-center text-gray-500 dark:text-gray-400">
                      <ng-icon [name]="item.icon" class="size-5" />
                    </div>
                    <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
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
  `,
})
export class AdminNav implements OnInit {
  private marketplace = inject(AdminMarketplaceService);
  private userService = inject(UserService);
  protected sidenavService = inject(SidenavService);

  private readonly allNavGroups: NavGroup[] = [
    {
      label: 'Usage & Spend',
      items: [
        { label: 'Cost Analytics', icon: 'heroCurrencyDollar', route: '/admin/costs', scope: 'admin.costs' },
        // Same scope as Cost Analytics on purpose: the feedback arms are read
        // against the cost rows they join to, and there is no separate signal
        // to delegate independently.
        { label: 'Feedback', icon: 'heroHandThumbDown', route: '/admin/feedback', scope: 'admin.costs' },
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
        { label: 'Agent Templates', icon: 'heroSquares2x2', route: '/admin/agent-templates', scope: 'admin.agent_templates' },
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
        { label: 'Announcements', icon: 'heroMegaphone', route: '/admin/manage-announcements', scope: 'admin.announcements' },
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
   *
   * Fired once when the nav mounts, which is when the shell enters the console —
   * not once per admin page, because the nav now outlives the navigation between them.
   */
  ngOnInit(): void {
    // Gated on the scope: the counts endpoint is guarded by `admin.marketplace`,
    // so firing it unconditionally would mean a guaranteed 403 for an admin who
    // does not hold that scope — a console full of console-errors for a badge
    // they cannot see anyway.
    if (this.userService.hasAdminScope('admin.marketplace')) {
      void this.marketplace.refreshQueueCounts();
    }
  }
}
