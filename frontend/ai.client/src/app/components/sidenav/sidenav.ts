import { Component, inject, computed, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter } from 'rxjs/operators';
import { SessionList } from './components/session-list/session-list';
import { AdminNav } from '../../admin/admin-nav';
import { isAdminChromeRoute } from '../../shared/utils/route-chrome';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { UserDropdownComponent } from '../topnav/components/user-dropdown.component';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { TooltipDirective } from '../tooltip/tooltip.directive';
import { BrandingService } from '../../../branding/branding.service';

@Component({
  selector: 'app-sidenav',
  imports: [SessionList, AdminNav, UserDropdownComponent, TooltipDirective, RouterLink, RouterLinkActive],
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

  newSession() {
    this.sidenavService.close();
    this.router.navigate(['']);
  }

  navigateToAgents() {
    this.sidenavService.close();
    this.router.navigate(['/agents']);
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
