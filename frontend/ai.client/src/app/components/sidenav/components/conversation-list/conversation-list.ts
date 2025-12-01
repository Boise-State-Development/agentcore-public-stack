import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
@Component({
  selector: 'app-conversation-list',
  imports: [RouterLink, RouterLinkActive],
  templateUrl: './conversation-list.html',
  styleUrl: './conversation-list.css',
})
export class ConversationList {


  protected getConversationId(conversationId: string): string {
    return conversationId;
  }

}
