import { Component, inject, effect, Signal, signal, computed, OnDestroy } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';
import { MessageListComponent } from './components/message-list/message-list.component';
import { ChatRequestService } from './services/chat/chat-request.service';
import { MessageMapService } from './services/session/message-map.service';
import { Message } from './services/models/message.model';
import { ChatInputComponent } from './components/chat-input/chat-input.component';
import { SessionService } from './services/session/session.service';
import { ChatStateService } from './services/chat/chat-state.service';
import { JsonPipe } from '@angular/common';
import { LoadingComponent } from '../components/loading.component';

@Component({
  selector: 'app-session-page',
  imports: [ChatInputComponent, MessageListComponent, JsonPipe, LoadingComponent],
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
  private chatStateService = inject(ChatStateService);
  private routeSubscription?: Subscription;
  readonly sessionConversation = this.sessionService.currentSession;
  readonly isChatLoading = this.chatStateService.isChatLoading;

  // Computed signal to check if session has messages
  readonly hasMessages = computed(() => this.messages().length > 0);

  constructor() {
    // Subscribe to route parameter changes
    this.routeSubscription = this.route.paramMap.subscribe(async params => {
      const id = params.get('sessionId');
      this.sessionId.set(id);
      if (id) {
        this.messages = this.messageMapService.getMessagesForSession(id);

        // Trigger fetching session metadata to populate currentSession
        this.sessionService.setSessionMetadataId(id);

        // Load messages from API for deep linking support
        try {
          await this.messageMapService.loadMessagesForSession(id);
        } catch (error) {
          console.error('Failed to load messages for session:', id, error);
        }
      } else {
        // No session selected, clear the session metadata
        this.sessionService.setSessionMetadataId(null);
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
