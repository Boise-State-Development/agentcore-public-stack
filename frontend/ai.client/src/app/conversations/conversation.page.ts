import { Component } from '@angular/core';
import { ChatInputComponent } from './components/chat-input/chat-input.component';
@Component({
  selector: 'app-conversation-page',
  standalone: true,
  imports: [ChatInputComponent],
  templateUrl: './conversation.page.html',
  styleUrl: './conversation.page.css'
})
export class ConversationPage {
}

