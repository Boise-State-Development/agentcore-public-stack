import { Component, inject, computed } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { SessionList } from './components/session-list/session-list';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { UserDropdownComponent } from '../topnav/components/user-dropdown.component';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { TooltipDirective } from '../tooltip/tooltip.directive';

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

  // Check if user has system_admin AppRole (resolved from backend RBAC)
  protected isAdmin = this.userService.isAdmin;

  newSession() {
    this.sidenavService.close();
    this.router.navigate(['']);
  }

  navigateToAssistants() {
    this.sidenavService.close();
    this.router.navigate(['/assistants']);
  }

  toggleCollapse() {
    this.sidenavService.toggleCollapsed();
  }

  async handleLogout(): Promise<void> {
    // BFF logout: 204 + cleared cookies. The user is then anonymous;
    // the next page navigation triggers SessionService.bootstrap() in
    // APP_INITIALIZER, which 401s and redirects to BFF /auth/login.
    // We push the user to /login explicitly here so the redirect-to-OAuth
    // happens from a known route rather than wherever the sidenav lived.
    await this.bffSession.logout();
    this.router.navigate(['/auth/login']);
  }
}
