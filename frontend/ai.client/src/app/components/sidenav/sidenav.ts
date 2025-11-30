import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, Router } from '@angular/router';
@Component({
  selector: 'app-sidenav',
  imports: [RouterLink, RouterLinkActive,],
  templateUrl: './sidenav.html',
  styleUrl: './sidenav.css',
})
export class Sidenav {
  router = inject(Router);
  constructor() {}

  protected getConversationId(conversationId: string): string {
    return conversationId;
  }

  newSession() {
    this.router.navigate(['']);
  }

}
