import { Component, input, signal, computed, model, inject, effect, ViewChild, ElementRef, AfterViewChecked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { TestChatService } from '../../services/test-chat.service';
import { Document } from '../../models/document.model';

interface SimpleMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  isStreaming?: boolean;
}

@Component({
  selector: 'app-test-chat',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './test-chat.component.html',
  styleUrl: './test-chat.component.css'
})
export class TestChatComponent implements AfterViewChecked {
  private testChatService = inject(TestChatService);

  // Inputs
  assistantId = input.required<string>();
  documents = input.required<Document[]>();

  // State
  messages = signal<SimpleMessage[]>([]);
  isLoading = signal<boolean>(false);
  userInput = model<string>(''); // Use model() for two-way binding with ngModel
  private currentAssistantMessage: SimpleMessage | null = null;
  private abortController: AbortController | null = null;

  // Computed
  isEnabled = computed(() => {
    const docs = this.documents();
    return docs.some(doc => doc.status === 'complete');
  });

  hasProcessedDocuments = computed(() => {
    const docs = this.documents();
    return docs.some(doc => doc.status === 'complete');
  });

  @ViewChild('messagesContainer', { static: false }) messagesContainer?: ElementRef<HTMLDivElement>;
  private shouldScroll = false;

  ngAfterViewChecked(): void {
    if (this.shouldScroll) {
      this.scrollToBottom();
      this.shouldScroll = false;
    }
  }

  scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const element = this.messagesContainer.nativeElement;
      element.scrollTop = element.scrollHeight;
    }
  }

  async sendMessage(): Promise<void> {
    const message = this.userInput().trim();
    if (!message || this.isLoading() || !this.isEnabled()) {
      return;
    }

    // Add user message
    const userMessage: SimpleMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: message
    };
    this.messages.update(msgs => [...msgs, userMessage]);
    this.userInput.set('');
    this.shouldScroll = true;

    // Create placeholder for assistant response
    const assistantMessageId = `assistant-${Date.now()}`;
    this.currentAssistantMessage = {
      id: assistantMessageId,
      role: 'assistant',
      content: '',
      isStreaming: true
    };
    this.messages.update(msgs => [...msgs, this.currentAssistantMessage!]);
    this.isLoading.set(true);
    this.shouldScroll = true;

    // Abort any previous request
    if (this.abortController) {
      this.abortController.abort();
    }
    this.abortController = new AbortController();

    try {
      await this.testChatService.sendTestChatMessageWithCallbacks(
        this.assistantId(),
        message,
        {
          onEvent: (event: string, data: any) => {
            this.handleStreamEvent(event, data);
          },
          onError: (error: Error) => {
            console.error('Test chat error:', error);
            this.handleError(error);
          },
          onClose: () => {
            this.isLoading.set(false);
            if (this.currentAssistantMessage) {
              this.currentAssistantMessage.isStreaming = false;
              this.currentAssistantMessage = null;
            }
          }
        }
      );
    } catch (error) {
      console.error('Test chat request failed:', error);
      this.handleError(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private handleStreamEvent(event: string, data: any): void {
    // Handle debug events even when no assistant message exists yet
    if (event === 'debug') {
      if (data.type === 'rag_debug') {
        console.log(`[RAG Debug] Found ${data.chunk_count} chunks:`, data.chunks);
        // Optionally display debug info in UI (for now just log to console)
      }
      return;
    }

    if (!this.currentAssistantMessage) {
      return;
    }

    switch (event) {
      case 'message_start':
        // Message started, ensure we have the message
        if (data.role === 'assistant') {
          this.currentAssistantMessage.content = '';
        }
        break;

      case 'content_block_start':
        // Content block started
        if (data.type === 'text') {
          this.currentAssistantMessage.content = '';
        }
        break;

      case 'content_block_delta':
        // Append text delta
        if (data.type === 'text' && data.text) {
          this.currentAssistantMessage.content += data.text;
          this.updateCurrentMessage();
          this.shouldScroll = true;
        }
        break;

      case 'content_block_stop':
        // Content block completed
        break;

      case 'message_stop':
        // Message completed
        this.currentAssistantMessage.isStreaming = false;
        this.updateCurrentMessage();
        this.isLoading.set(false);
        this.shouldScroll = true;
        break;

      case 'done':
        // Stream completed
        this.isLoading.set(false);
        if (this.currentAssistantMessage) {
          this.currentAssistantMessage.isStreaming = false;
          this.updateCurrentMessage();
        }
        this.shouldScroll = true;
        break;

      case 'error':
        // Error occurred
        const errorMessage = data.message || data.error || 'An error occurred';
        this.currentAssistantMessage.content = `Error: ${errorMessage}`;
        this.currentAssistantMessage.isStreaming = false;
        this.updateCurrentMessage();
        this.isLoading.set(false);
        this.shouldScroll = true;
        break;

      default:
        // Ignore unknown events
        break;
    }
  }

  private updateCurrentMessage(): void {
    if (!this.currentAssistantMessage) {
      return;
    }

    this.messages.update(msgs => {
      const index = msgs.findIndex(msg => msg.id === this.currentAssistantMessage!.id);
      if (index >= 0) {
        const updated = [...msgs];
        updated[index] = { ...this.currentAssistantMessage! };
        return updated;
      }
      return msgs;
    });
  }

  private handleError(error: Error): void {
    if (this.currentAssistantMessage) {
      this.currentAssistantMessage.content = `Error: ${error.message}`;
      this.currentAssistantMessage.isStreaming = false;
      this.updateCurrentMessage();
    } else {
      // Add error message if no assistant message exists
      const errorMessage: SimpleMessage = {
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: `Error: ${error.message}`
      };
      this.messages.update(msgs => [...msgs, errorMessage]);
    }
    this.isLoading.set(false);
    this.shouldScroll = true;
  }

  clearChat(): void {
    this.messages.set([]);
    this.currentAssistantMessage = null;
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
    this.isLoading.set(false);
  }

  onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.sendMessage();
    }
  }
}

