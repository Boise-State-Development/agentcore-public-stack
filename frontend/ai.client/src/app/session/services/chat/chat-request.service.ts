import { inject, Injectable } from '@angular/core';
import { Router } from '@angular/router';
import { v4 as uuidv4 } from 'uuid';
import { ChatStateService } from './chat-state.service';
import { ChatHttpService } from './chat-http.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { UserService } from '../../../auth/user.service';
import { ModelService } from '../model/model.service';
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
  private sessionService = inject(SessionService);
  private userService = inject(UserService);
  private modelService = inject(ModelService);
  private router = inject(Router);
  // TODO: Inject proper logging service
  
  async submitChatRequest(userInput: string, sessionId: string | null): Promise<void> {
    // Ensure conversation exists and get its ID
    // Update URL to reflect current conversation
    const isNewSession = !sessionId;
    sessionId = sessionId || uuidv4();

    this.navigateToSession(sessionId);

    // If this is a new session, add it to the session cache optimistically
    if (isNewSession) {
      // Get the current user from UserService
      const user = this.userService.getUser();
      const userId = user?.empl_id || 'anonymous';

      // Add the new session to the cache so it appears in the sidenav immediately
      this.sessionService.addSessionToCache(sessionId, userId);
    }

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
    const selectedModel = this.modelService.getSelectedModel();

    if (!selectedModel) {
      throw new Error('No model selected. Please select a model before sending a message.');
    }

    // If using the system default model, send null for model_id to let backend use its default
    const isDefaultModel = this.modelService.isUsingDefaultModel();
    
    return {
      message,
      session_id,
      model_id: isDefaultModel ? null : selectedModel.modelId,
      enabled_tools: ['calculator', 'fetch_url_content', 'generate_diagram_and_validate', 'search_boise_state'],
      provider: isDefaultModel ? null : selectedModel.provider
    };
  }
}