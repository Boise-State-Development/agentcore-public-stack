import { Component, input } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { Message } from '../../services/models/message.model';
import { UserMessageComponent } from './components/user-message.component';
import { AssistantMessageComponent } from './components/assistant-message.component';
import { JsonSyntaxHighlightPipe } from './components/tool-use';

@Component({
  selector: 'app-message-list',
  imports: [JsonPipe, UserMessageComponent, AssistantMessageComponent, JsonSyntaxHighlightPipe],
  templateUrl: './message-list.component.html',
  styleUrl: './message-list.component.css',
})
export class MessageListComponent {
  messages = input.required<Message[]>();

 
}

