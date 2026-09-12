import { Component, inject, computed, signal } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { SessionList } from './components/session-list/session-list';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { UserDropdownComponent } from '../topnav/components/user-dropdown.component';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { TooltipDirective } from '../tooltip/tooltip.directive';
import { BrandingService } from '../../../branding/branding.service';

@Component({
  selector: 'app-sidenav',
  imports: [SessionList, UserDropdownComponent, TooltipDirective, RouterLink, RouterLinkActive],
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
