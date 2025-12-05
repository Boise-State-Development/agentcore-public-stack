import { inject, Injectable } from '@angular/core';
import { Router } from '@angular/router';
import { v4 as uuidv4 } from 'uuid';
import { ChatStateService } from './chat-state.service';
import { ChatHttpService } from './chat-http.service';
import { MessageMapService } from '../session/message-map.service';
// import { ConversationService, Conversation } from '../conversation/conversation.service';
// import { createMessage, createTextContent } from '../models';

export interface ContentFile {
  fileName: string;
  fileSize: number;
  contentType: string;
  s3Key: string;
}

@Injectable({
  providedIn: 'root'
})
export class ChatRequestService {
  // private conversationService = inject(ConversationService);
  private chatHttpService = inject(ChatHttpService);
  private chatStateService = inject(ChatStateService);
  private messageMapService = inject(MessageMapService);
  private router = inject(Router);
  // TODO: Inject proper logging service
  
  async submitChatRequest(userInput: string, sessionId: string | null): Promise<void> {
    // Ensure conversation exists and get its ID
    // Update URL to reflect current conversation
    sessionId = sessionId || uuidv4();
    this.navigateToSession(sessionId);

    // Create and add user message
    this.messageMapService.addUserMessage(sessionId, userInput);
    
    // Start streaming for this conversation
    this.messageMapService.startStreaming(sessionId);
    
    // Build and send request
    const requestObject = this.buildChatRequestObject(userInput, sessionId);

    try {
      await this.chatHttpService.sendChatRequest(requestObject);
    } catch (error) {
      // TODO: Replace with proper logging service
      // logger.error('Chat request failed', { error, conversationId: sessionId });
      this.chatStateService.setChatLoading(false);
      this.messageMapService.endStreaming();
      throw error; // Re-throw to allow caller to handle
    }
  }

  /**
   * Navigates to the conversation route
   * @param sessionId The conversation ID to navigate to
   */
  private navigateToSession(sessionId: string): void {
    // Using replaceUrl to avoid polluting browser history
    this.router.navigate(['s', sessionId], { replaceUrl: true });
  }

  private buildChatRequestObject(message: string, session_id: string) {
    return {
      message,
      session_id,
      enabled_tools: ['calculator']
    };
  }
}