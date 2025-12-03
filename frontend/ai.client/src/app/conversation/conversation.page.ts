import { Component, inject, effect, Signal, signal, OnDestroy } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';
import { MessageListComponent } from './components/message-list/message-list.component';
import { ChatRequestService } from './services/chat/chat-request.service';
import { MessageMapService } from './services/conversation/message-map.service';
import { Message } from './services/models/message.model';
import { ChatInputComponent } from './components/chat-input/chat-input.component';
import { ConversationService } from './services/conversation/conversation.service';

@Component({
  selector: 'app-conversation-page',
  imports: [ChatInputComponent, MessageListComponent],
  templateUrl: './conversation.page.html',
  styleUrl: './conversation.page.css',
})
export class ConversationPage implements OnDestroy {
  messages: Signal<Message[]> = signal([]);
  conversationId = signal<string | null>(null);

  private route = inject(ActivatedRoute);
  private conversationService = inject(ConversationService);
  private chatRequestService = inject(ChatRequestService);
  private messageMapService = inject(MessageMapService);
  private routeSubscription?: Subscription;
  readonly currentConversation = this.conversationService.currentConversation;

  constructor() {
    // Subscribe to route parameter changes
    this.routeSubscription = this.route.paramMap.subscribe(params => {
      const id = params.get('conversationId');
      this.conversationId.set(id);
      if (id) {
        this.messages = this.messageMapService.getMessagesForConversation(id);
      }
    });
  }

  ngOnDestroy() {
    this.routeSubscription?.unsubscribe();
  }

  onMessageSubmitted(message: { content: string, timestamp: Date }) {
    this.chatRequestService.submitChatRequest(message.content).catch((error) => {
      console.error('Error sending chat request:', error);
    });
  }

  onFileAttached(file: File) {
    console.log('File attached:', file);
    // Handle file attachment logic here
  }
}
