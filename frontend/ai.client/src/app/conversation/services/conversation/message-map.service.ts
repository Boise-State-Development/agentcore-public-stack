// services/message-map.service.ts (updated integration)
import { Injectable, Signal, WritableSignal, signal, effect, inject } from '@angular/core';
import { Message } from '../models/message.model';
import { StreamParserService } from '../chat/stream-parser.service';
interface MessageMap {
  [conversationId: string]: WritableSignal<Message[]>;
}

@Injectable({
  providedIn: 'root'
})
export class MessageMapService {
  private messageMap = signal<MessageMap>({});
  private activeStreamConversationId = signal<string | null>(null);
  
  private streamParser = inject(StreamParserService);
  
  constructor() {
    // Reactive effect: automatically sync streaming messages to the message map
    effect(() => {
      const conversationId = this.activeStreamConversationId();
      const streamMessages = this.streamParser.allMessages();
      
      if (conversationId && streamMessages.length > 0) {
        this.syncStreamingMessages(conversationId, streamMessages);
      }
    });
  }
  
  /**
   * Start streaming for a conversation.
   * Call this before beginning to parse SSE events.
   */
  startStreaming(conversationId: string): void {
    this.activeStreamConversationId.set(conversationId);
    this.streamParser.reset();
    
    // Ensure the conversation exists in the map
    if (!this.messageMap()[conversationId]) {
      this.messageMap.update(map => ({
        ...map,
        [conversationId]: signal<Message[]>([])
      }));
    }
  }
  
  /**
   * End streaming for the current conversation.
   * Finalizes messages and clears streaming state.
   */
  endStreaming(): void {
    const conversationId = this.activeStreamConversationId();
    if (conversationId) {
      // Ensure final messages are synced
      const finalMessages = this.streamParser.allMessages();
      if (finalMessages.length > 0) {
        this.syncStreamingMessages(conversationId, finalMessages);
      }
    }
    
    this.activeStreamConversationId.set(null);
  }
  
  /**
   * Get the messages signal for a conversation.
   */
  getMessagesForConversation(conversationId: string): Signal<Message[]> {
    const existing = this.messageMap()[conversationId];
    if (existing) {
      return existing;
    }
    
    const newSignal = signal<Message[]>([]);
    this.messageMap.update(map => ({
      ...map,
      [conversationId]: newSignal
    }));
    
    return newSignal;
  }
  
  /**
   * Add a user message to a conversation (before streaming begins).
   */
  addUserMessage(conversationId: string, content: string): Message {
    const message: Message = {
      id: crypto.randomUUID(),
      role: 'user',
      content: [{ type: 'text', text: content }]
    };
    
    this.messageMap.update(map => {
      const updated = { ...map };
      
      if (!updated[conversationId]) {
        updated[conversationId] = signal([message]);
      } else {
        updated[conversationId].update(msgs => [...msgs, message]);
      }
      
      return updated;
    });
    
    return message;
  }
  
  /**
   * Sync streaming messages to the message map.
   * Handles the case where we're appending to existing messages.
   * Preserves all previous complete messages and only replaces the currently streaming assistant response.
   */
  private syncStreamingMessages(conversationId: string, streamMessages: Message[]): void {
    const conversationSignal = this.messageMap()[conversationId];
    if (!conversationSignal) return;
    
    conversationSignal.update(existingMessages => {
      // Find the index of the last user message
      let lastUserMessageIndex = -1;
      for (let i = existingMessages.length - 1; i >= 0; i--) {
        if (existingMessages[i].role === 'user') {
          lastUserMessageIndex = i;
          break;
        }
      }
      
      // If no user message found, just return the stream messages
      if (lastUserMessageIndex === -1) {
        return streamMessages;
      }
      
      // Keep all messages up to and including the last user message
      // Then append the streaming messages (replacing any partial assistant messages after the last user message)
      const preservedMessages = existingMessages.slice(0, lastUserMessageIndex + 1);
      return [...preservedMessages, ...streamMessages];
    });
  }
  
  /**
   * Clear all data for a conversation.
   */
  clearConversation(conversationId: string): void {
    this.messageMap.update(map => {
      const { [conversationId]: _, ...rest } = map;
      return rest;
    });
  }

}