import { Component, inject, computed, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter } from 'rxjs/operators';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroAdjustmentsHorizontal,
  heroChevronRight,
  heroClock,
  heroDocumentText,
  heroFolderOpen,
  heroSparkles,
} from '@ng-icons/heroicons/outline';
import { SessionList } from './components/session-list/session-list';
import { AdminNav } from '../../admin/admin-nav';
import { isAdminChromeRoute } from '../../shared/utils/route-chrome';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { UserDropdownComponent } from '../topnav/components/user-dropdown.component';
import { NotificationBellComponent } from '../notification-bell/notification-bell.component';
import { FEATURES } from '../../services/features';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { TooltipDirective } from '../tooltip/tooltip.directive';
import { BrandingService } from '../../../branding/branding.service';
import { SearchDialogService } from '../search/search-dialog.service';
import { searchShortcutAria, searchShortcutLabel } from '../search/search-shortcut';
import { SidebarLayoutService } from './sidebar-layout.service';
import { EditSidebarDialogComponent } from './components/edit-sidebar-dialog.component';

@Component({
  selector: 'app-sidenav',
  imports: [
    SessionList,
    AdminNav,
    UserDropdownComponent,
    NotificationBellComponent,
    TooltipDirective,
    RouterLink,
    RouterLinkActive,
    CdkMenuTrigger,
    CdkMenu,
    CdkMenuItem,
    NgIcon,
  ],
  // Every icon an entry in `sidebar-items.ts` names, plus More's chevron.
  providers: [
    provideIcons({
      heroAdjustmentsHorizontal,
      heroChevronRight,
      heroClock,
      heroDocumentText,
      heroFolderOpen,
      heroSparkles,
    }),
  ],
  templateUrl: './sidenav.html',
  styleUrl: './sidenav.css',
})
export class Sidenav {
  private router = inject(Router);
  private sessionService = inject(SessionService);
  private bffSession = inject(BffSessionService);
  protected sidenavService = inject(SidenavService);
  protected userService = inject(UserService);
  protected branding = inject(BrandingService);
  /** This build's front-end feature switches (compile-time; see environments/feature-flags.ts). */
  protected readonly features = inject(FEATURES);
  private readonly searchDialog = inject(SearchDialogService);
  private readonly dialog = inject(Dialog);
  /** Which navigation entries the user shows, in what order (Edit sidebar). */
  protected readonly sidebarLayout = inject(SidebarLayoutService);

  /** Beside the More row where there is room; under or over it in a narrow drawer. */
  protected readonly moreMenuPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'top', offsetX: 8 },
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'bottom', offsetX: 8 },
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 4 },
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -4 },
  ];

  /** The search shortcut as this platform spells it, for the button's tooltip. */
  protected readonly searchShortcutLabel = searchShortcutLabel();
  protected readonly searchShortcutAria = searchShortcutAria();

  /** Whether the branding logo image failed to load (Requirement 2.8). */
  protected logoLoadFailed = signal(false);

  /** Whether the chat body has scrolled off its top, which reveals the fade
   *  under the pinned New Session button. */
  protected bodyScrolled = signal(false);

  /** Re-read on every completed navigation; the value itself is unused,
   *  it exists so `isAdminChrome` recomputes when the route changes. */
  private readonly navigated = toSignal(
    this.router.events.pipe(filter((e) => e instanceof NavigationEnd)),
    { initialValue: null },
  );

  /**
   * Whether the active route is inside the admin console, in which case the
   * sidenav's body is the console's navigation instead of the chat one.
   *
   * Derived from the route's `chrome` flag rather than a `/admin` URL test:
   * the shell already reads that flag to decide the content box, and two
   * independent answers to "are we in the console?" is one more than can stay
   * in agreement.
   */
  protected readonly isAdminChrome = computed(() => {
    this.navigated();
    return isAdminChromeRoute(this.router.routerState.snapshot.root);
  });

  /** Whether the active page is one of the entries waiting under More. */
  protected readonly moreActive = computed(() => {
    this.navigated();
    const url = (this.router.url ?? '').split(/[?#]/)[0];
    return this.sidebarLayout
      .hiddenEntries()
      .some((entry) => url === entry.route || url.startsWith(`${entry.route}/`));
  });

  // Access to current session signals - available for use in template or component logic
  readonly currentSession = this.sessionService.currentSession;
  readonly hasCurrentSession = this.sessionService.hasCurrentSession;

  // Expose collapsed state for template
  readonly isCollapsed = this.sidenavService.isCollapsed;

  // Example: Computed signal for display purposes
  readonly currentSessionTitle = computed(() => {
    const session = this.currentSession();
    return session.title || 'Untitled Session';
  });

  /**
   * Whether to offer the "Admin Dashboard" entry point.
   *
   * `canAccessAdmin`, not `isAdmin`: a delegated admin holds no `system_admin`
   * AppRole but does have somewhere to go inside the console, and hiding the
   * link would leave them typing `/admin` by hand.
   */
  protected isAdmin = this.userService.canAccessAdmin;

  /** Opens conversation search; the drawer closes first so the dialog is not under it on mobile. */
  protected openSearch(): void {
    this.sidenavService.close();
    void this.searchDialog.open();
  }

  newSession() {
    this.sidenavService.close();
    this.router.navigate(['']);
  }

  navigateToAgents() {
    this.sidenavService.close();
    this.router.navigate(['/agents']);
  }

  /**
   * Opens Edit sidebar. Its changes show in the sidebar as they are made; the
   * layout is saved once, however the dialog closes. A failed save surfaces
   * through the global error toast and the sidebar keeps the edit.
   */
  protected openEditSidebar(): void {
    this.dialog
      .open<void>(EditSidebarDialogComponent)
      .closed.subscribe(() => void this.sidebarLayout.save().catch(() => undefined));
  }

  onBodyScroll(event: Event): void {
    this.bodyScrolled.set((event.target as HTMLElement).scrollTop > 0);
  }

  toggleCollapse() {
    this.sidenavService.toggleCollapsed();
  }

  /**
   * Handles a branding logo `<img>` failing to load (missing/broken asset at
   * its documented path). Sets `logoLoadFailed`, which the template uses to
   * hide the broken `<img>` elements and reveal a same-dimension placeholder
   * with a visible "logo failed to load" indication, without collapsing the
   * layout (Requirement 2.8).
   */
  onLogoError(_event: Event): void {
    this.logoLoadFailed.set(true);
  }

  async handleLogout(): Promise<void> {
    // BFF logout clears cookies and bounces through the Cognito Hosted UI
    // logout URL (handled inside bffSession.logout). We also push the user
    // to /auth/login defensively in case the navigation is short-circuited.
    await this.bffSession.logout();
    this.router.navigate(['/auth/login']);
  }
}
