import { Component, input } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { Message } from '../../services/models/message.model';
import { UserMessageComponent } from './components/user-message.component';
import { AssistantMessageComponent } from './components/assistant-message.component';
import { MessageMetadataBadgesComponent } from './components/message-metadata-badges.component';

@Component({
  selector: 'app-message-list',
  imports: [JsonPipe, UserMessageComponent, AssistantMessageComponent, MessageMetadataBadgesComponent],
  templateUrl: './message-list.component.html',
  styleUrl: './message-list.component.css',
})
export class MessageListComponent {
  messages = input.required<Message[]>();

 
}

