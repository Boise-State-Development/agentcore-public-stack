import { Component } from '@angular/core';
import { ThemeToggleComponent } from '../theme-toggle/theme-toggle.component';
import { RouterLink } from '@angular/router';
@Component({
  selector: 'app-topnav',
  imports: [ThemeToggleComponent, RouterLink],
  templateUrl: './topnav.html',
  styleUrl: './topnav.css',
})
export class Topnav {

 

}
