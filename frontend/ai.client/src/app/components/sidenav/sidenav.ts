import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, Router } from '@angular/router';
import { ConversationList } from './components/conversation-list/conversation-list';
@Component({
  selector: 'app-sidenav',
  imports: [RouterLink, ConversationList],
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
