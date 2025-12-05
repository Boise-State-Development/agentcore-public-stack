// services/message-map.service.ts (updated integration)
import { Injectable, Signal, WritableSignal, signal, effect, inject } from '@angular/core';
import { Message } from '../models/message.model';
import { StreamParserService } from '../chat/stream-parser.service';
interface MessageMap {
  [sessionId: string]: WritableSignal<Message[]>;
}

@Injectable({
  providedIn: 'root'
})
export class MessageMapService {
  private messageMap = signal<MessageMap>({});
  private activeStreamSessionId = signal<string | null>(null);
  
  private streamParser = inject(StreamParserService);
  
  constructor() {
    // Reactive effect: automatically sync streaming messages to the message map
    effect(() => {
      const sessionId = this.activeStreamSessionId();
      const streamMessages = this.streamParser.allMessages();
      
      if (sessionId && streamMessages.length > 0) {
        this.syncStreamingMessages(sessionId, streamMessages);
      }
    });
  }
  
  /**
   * Start streaming for a session.
   * Call this before beginning to parse SSE events.
   */
  startStreaming(sessionId: string): void {
    this.activeStreamSessionId.set(sessionId);
    this.streamParser.reset();
    
    // Ensure the session exists in the map
    if (!this.messageMap()[sessionId]) {
      this.messageMap.update(map => ({
        ...map,
        [sessionId]: signal<Message[]>([])
      }));
    }
  }
  
  /**
   * End streaming for the current session.
   * Finalizes messages and clears streaming state.
   */
  endStreaming(): void {
    const sessionId = this.activeStreamSessionId();
    if (sessionId) {
      // Ensure final messages are synced
      const finalMessages = this.streamParser.allMessages();
      if (finalMessages.length > 0) {
        this.syncStreamingMessages(sessionId, finalMessages);
      }
    }
    
    this.activeStreamSessionId.set(null);
  }
  
  /**
   * Get the messages signal for a session.
   */
  getMessagesForSession(sessionId: string): Signal<Message[]> {
    const existing = this.messageMap()[sessionId];
    if (existing) {
      return existing;
    }
    
    const newSignal = signal<Message[]>([]);
    this.messageMap.update(map => ({
      ...map,
      [sessionId]: newSignal
    }));
    
    return newSignal;
  }
  
  /**
   * Add a user message to a session (before streaming begins).
   */
  addUserMessage(sessionId: string, content: string): Message {
    const message: Message = {
      id: crypto.randomUUID(),
      role: 'user',
      content: [{ type: 'text', text: content }]
    };
    
    this.messageMap.update(map => {
      const updated = { ...map };
      
      if (!updated[sessionId]) {
        updated[sessionId] = signal([message]);
      } else {
        updated[sessionId].update(msgs => [...msgs, message]);
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
  private syncStreamingMessages(sessionId: string, streamMessages: Message[]): void {
    const sessionSignal = this.messageMap()[sessionId];
    if (!sessionSignal) return;
    
    sessionSignal.update(existingMessages => {
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
   * Clear all data for a session.
   */
  clearSession(sessionId: string): void {
    this.messageMap.update(map => {
      const { [sessionId]: _, ...rest } = map;
      return rest;
    });
  }

}