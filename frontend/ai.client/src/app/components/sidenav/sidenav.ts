import { Component, effect, inject, computed } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { SessionList } from './components/session-list/session-list';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { UserDropdownComponent } from '../topnav/components/user-dropdown.component';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { TooltipDirective } from '../tooltip/tooltip.directive';
import { MemorySpaceService } from '../../memory-spaces/services/memory-space.service';
import { AgentService } from '../../agents/services/agent.service';

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
  private memorySpaceService = inject(MemorySpaceService);
  private agentService = inject(AgentService);
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

  /**
   * Whether to show the "Memory Spaces" nav entry. Rides the spaces list
   * call: a successful (even empty) list means the `MEMORY_SPACES_ENABLED`
   * kill switch is on; a 404 flips `accessible$` false and the entry stays
   * hidden. `null` (unresolved) also hides it so it doesn't flash in before
   * disappearing.
   */
  readonly showMemorySpaces = computed(() => this.memorySpaceService.accessible$() === true);

  /**
   * Whether to show the "Agents" (Agent Designer) nav entry. Rides the agents
   * list call the same way `showMemorySpaces` rides spaces: a successful (even
   * empty) list means the `AGENTS_API_ENABLED` kill switch is on; a 404 flips
   * `accessible$` false and the entry stays hidden. `null` (unresolved) also
   * hides it so it doesn't flash in before disappearing.
   */
  readonly showAgents = computed(() => this.agentService.accessible$() === true);

  constructor() {
    // Kick off the accessibility probes once the user is authenticated —
    // mirrors UserService's own permissions-fetch gating.
    effect(() => {
      if (this.userService.currentUser()) {
        void this.memorySpaceService.loadSpaces();
        void this.agentService.loadAgents();
      }
    });
  }

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
    // BFF logout clears cookies and bounces through the Cognito Hosted UI
    // logout URL (handled inside bffSession.logout). We also push the user
    // to /auth/login defensively in case the navigation is short-circuited.
    await this.bffSession.logout();
    this.router.navigate(['/auth/login']);
  }
}
