import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, Router } from '@angular/router';
import { SessionList } from './components/session-list/session-list';
@Component({
  selector: 'app-sidenav',
  imports: [RouterLink, SessionList],
  templateUrl: './sidenav.html',
  styleUrl: './sidenav.css',
})
export class Sidenav {
  router = inject(Router);
  constructor() {}

  newSession() {
    this.router.navigate(['']);
  }
}
