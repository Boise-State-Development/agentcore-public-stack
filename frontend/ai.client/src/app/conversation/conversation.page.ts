import { Component, inject } from '@angular/core';
import { ChatInputComponent } from './components/chat-input/chat-input.component';
import { ChatRequestService } from './services/chat/chat-request.service';
@Component({
  selector: 'app-conversation-page',
  standalone: true,
  imports: [ChatInputComponent],
  templateUrl: './conversation.page.html',
  styleUrl: './conversation.page.css'
})
export class ConversationPage {
    private chatRequestService = inject(ChatRequestService);

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

