import { Component, inject, computed } from '@angular/core';
import { Router } from '@angular/router';
import { SessionList } from './components/session-list/session-list';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { AuthService } from '../../auth/auth.service';
import { UserDropdownComponent } from '../topnav/components/user-dropdown.component';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { TooltipDirective } from '../tooltip/tooltip.directive';

@Component({
  selector: 'app-sidenav',
  imports: [SessionList, UserDropdownComponent, TooltipDirective],
  templateUrl: './sidenav.html',
  styleUrl: './sidenav.css',
})
export class Sidenav {
  private router = inject(Router);
  private sessionService = inject(SessionService);
  private authService = inject(AuthService);
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

  // Check if user has admin roles
  protected isAdmin = computed(() => {
    const requiredRoles = ['Admin', 'SuperAdmin', 'DotNetDevelopers'];
    return this.userService.hasAnyRole(requiredRoles);
  });

  newSession() {
    this.sidenavService.close();
    this.router.navigate(['']);
  }

  toggleCollapse() {
    this.sidenavService.toggleCollapsed();
  }

  handleLogout() {
    // Redirect to home page after logout
    const postLogoutRedirectUri = window.location.origin;
    this.authService.logout(postLogoutRedirectUri);
  }
}
