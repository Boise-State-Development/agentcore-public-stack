import { Component, inject } from '@angular/core';
import { ThemeToggleComponent } from './components/theme-toggle/theme-toggle.component';
import { UserService } from '../../auth/user.service';
import { SessionService } from '../../session/services/session/session.service';
import { CommonModule } from '@angular/common';
import { UserDropdownComponent } from './components/user-dropdown.component';

@Component({
  selector: 'app-topnav',
  imports: [ThemeToggleComponent, CommonModule, UserDropdownComponent],
  templateUrl: './topnav.html',
  styleUrl: './topnav.css',
})
export class Topnav {
  protected userService = inject(UserService);
  protected sessionService = inject(SessionService);
  readonly currentSession = this.sessionService.currentSession;
}
