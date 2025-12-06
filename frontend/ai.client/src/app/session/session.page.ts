import { Component, inject, effect, Signal, signal, OnDestroy } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';
import { MessageListComponent } from './components/message-list/message-list.component';
import { ChatRequestService } from './services/chat/chat-request.service';
import { MessageMapService } from './services/session/message-map.service';
import { Message } from './services/models/message.model';
import { ChatInputComponent } from './components/chat-input/chat-input.component';
import { SessionService } from './services/session/session.service';

@Component({
  selector: 'app-session-page',
  imports: [ChatInputComponent, MessageListComponent],
  templateUrl: './session.page.html',
  styleUrl: './session.page.css',
})
export class ConversationPage implements OnDestroy {
  messages: Signal<Message[]> = signal([]);
  sessionId = signal<string | null>(null);

  private route = inject(ActivatedRoute);
  private sessionService = inject(SessionService);
  private chatRequestService = inject(ChatRequestService);
  private messageMapService = inject(MessageMapService);
  private routeSubscription?: Subscription;
  readonly sessionConversation = this.sessionService.currentSession;

  constructor() {
    // Subscribe to route parameter changes
    this.routeSubscription = this.route.paramMap.subscribe(async params => {
      const id = params.get('sessionId');
      this.sessionId.set(id);
      if (id) {
        this.messages = this.messageMapService.getMessagesForSession(id);

        // Load messages from API for deep linking support
        try {
          await this.messageMapService.loadMessagesForSession(id);
        } catch (error) {
          console.error('Failed to load messages for session:', id, error);
        }
      }
    });
  }

  ngOnDestroy() {
    this.routeSubscription?.unsubscribe();
  }

  onMessageSubmitted(message: { content: string, timestamp: Date }) {
    this.chatRequestService.submitChatRequest(message.content, this.sessionId()).catch((error) => {
      console.error('Error sending chat request:', error);
    });
  }

  onFileAttached(file: File) {
    console.log('File attached:', file);
    // Handle file attachment logic here
  }
}
