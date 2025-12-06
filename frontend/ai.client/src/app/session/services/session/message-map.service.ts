// services/message-map.service.ts (updated integration)
import { Injectable, Signal, WritableSignal, signal, effect, inject } from '@angular/core';
import { Message } from '../models/message.model';
import { StreamParserService } from '../chat/stream-parser.service';
import { SessionService } from './session.service';

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
  private sessionService = inject(SessionService);
  
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
   * Load messages for a session from the API.
   * If messages already exist in the map, they won't be reloaded.
   *
   * @param sessionId - The session ID to load messages for
   * @returns Promise that resolves when messages are loaded
   */
  async loadMessagesForSession(sessionId: string): Promise<void> {
    // Check if messages already exist
    const existingMessages = this.messageMap()[sessionId];
    if (existingMessages && existingMessages().length > 0) {
      // Messages already loaded, skip API call
      return;
    }

    try {
      // Fetch messages from the API
      const response = await this.sessionService.getMessages(sessionId);

      // Process messages to match tool results to tool uses
      const processedMessages = this.matchToolResultsToToolUses(response.messages);

      // Update the message map with loaded messages
      this.messageMap.update(map => {
        const updated = { ...map };

        if (!updated[sessionId]) {
          updated[sessionId] = signal(processedMessages);
        } else {
          updated[sessionId].set(processedMessages);
        }

        return updated;
      });
    } catch (error) {
      console.error('Failed to load messages for session:', sessionId, error);
      throw error;
    }
  }

  /**
   * Match tool results from user messages to their corresponding tool uses in assistant messages.
   * This reconstructs the tool state as it appears during streaming.
   *
   * The backend stores tool_use and tool_result as separate content blocks in separate messages:
   * - Assistant message: contains toolUse blocks
   * - User message: contains toolResult blocks
   *
   * This method merges the results into the toolUse blocks for proper UI display.
   *
   * @param messages - Array of messages from the API
   * @returns Processed messages with tool results embedded in tool uses
   */
  private matchToolResultsToToolUses(messages: Message[]): Message[] {
    // Create a map of toolUseId -> tool result for quick lookup
    const toolResultMap = new Map<string, { content: any[]; status: 'success' | 'error' }>();

    // First pass: collect all tool results
    for (const message of messages) {
      if (message.role === 'user') {
        for (const contentBlock of message.content) {
          if (contentBlock.type === 'toolResult' && contentBlock.toolResult) {
            const toolResult = contentBlock.toolResult as any;
            const toolUseId = toolResult.toolUseId;
            if (toolUseId) {
              toolResultMap.set(toolUseId, {
                content: toolResult.content || [],
                status: toolResult.status || 'success'
              });
            }
          }
        }
      }
    }

    // Second pass: match results to tool uses
    return messages.map(message => {
      if (message.role === 'assistant') {
        // Process content blocks to add results to tool uses
        const updatedContent = message.content.map(contentBlock => {
          if ((contentBlock.type === 'toolUse' || contentBlock.type === 'tool_use') && contentBlock.toolUse) {
            const toolUse = contentBlock.toolUse as any;
            const toolUseId = toolUse.toolUseId;

            // Check if we have a result for this tool use
            if (toolUseId && toolResultMap.has(toolUseId)) {
              const result = toolResultMap.get(toolUseId)!;

              // Create updated toolUse with embedded result
              return {
                ...contentBlock,
                toolUse: {
                  ...toolUse,
                  result: result,
                  status: result.status === 'error' ? 'error' : 'complete'
                }
              };
            }
          }
          return contentBlock;
        });

        return {
          ...message,
          content: updatedContent
        };
      }
      return message;
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