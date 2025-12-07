import { Component, inject } from '@angular/core';
import { ThemeToggleComponent } from '../theme-toggle/theme-toggle.component';
import { UserService } from '../../auth/user.service';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-topnav',
  imports: [ThemeToggleComponent, CommonModule],
  templateUrl: './topnav.html',
  styleUrl: './topnav.css',
})
export class Topnav {
  protected userService = inject(UserService);
}
