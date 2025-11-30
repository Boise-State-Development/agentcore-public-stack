import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';
@Component({
  selector: 'app-sidenav',
  imports: [RouterLink],
  templateUrl: './sidenav.html',
  styleUrl: './sidenav.css',
})
export class Sidenav {


  getConversationId(conversation: any): string {
    return ''
  }

}
