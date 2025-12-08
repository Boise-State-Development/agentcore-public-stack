import { Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { SessionList } from './components/session-list/session-list';
@Component({
  selector: 'app-sidenav',
  imports: [SessionList],
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
