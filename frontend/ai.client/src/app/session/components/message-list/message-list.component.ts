import { Component, input, signal, effect, OnDestroy, inject, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { Message } from '../../services/models/message.model';
import { UserMessageComponent } from './components/user-message.component';
import { AssistantMessageComponent } from './components/assistant-message.component';
import { MessageMetadataBadgesComponent } from './components/message-metadata-badges.component';
import { ThinkingDotsComponent } from '../../../components/thinking-dots.component';
import { JsonPipe } from '@angular/common';

@Component({
  selector: 'app-message-list',
  imports: [UserMessageComponent, AssistantMessageComponent, MessageMetadataBadgesComponent, ThinkingDotsComponent, JsonPipe],
  templateUrl: './message-list.component.html',
  styleUrl: './message-list.component.css',
})
export class MessageListComponent implements OnDestroy {
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);

  // Constants for scroll behavior and layout
  private readonly HEADER_HEIGHT = 64;
  private readonly SCROLL_PADDING = 16;
  private readonly RESIZE_DEBOUNCE_MS = 150;

  messages = input.required<Message[]>();
  isChatLoading = input<boolean>(false);

  // Calculate the spacer height dynamically
  // This creates space at the bottom so user messages can scroll to the top
  spacerHeight = signal(0);

  // Store debounced resize listener for cleanup
  private resizeListener = this.debounce(
    () => this.calculateSpacerHeight(),
    this.RESIZE_DEBOUNCE_MS
  );

  constructor() {
    if (this.isBrowser) {
      // Only recalculate when message count changes, not on every message update
      effect(() => {
        const messageCount = this.messages().length;
        this.calculateSpacerHeight();
      });

      // Add resize listener
      window.addEventListener('resize', this.resizeListener);
    }
  }

  ngOnDestroy() {
    if (this.isBrowser) {
      window.removeEventListener('resize', this.resizeListener);
    }
  }

  /**
   * Calculates the height needed for the bottom spacer
   * This ensures there's enough space for user messages to scroll to the top
   */
  private calculateSpacerHeight(): void {
    if (!this.isBrowser) return;

    // Wait for next frame to ensure DOM is updated
    requestAnimationFrame(() => {
      const viewportHeight = window.innerHeight;
      const spacerHeight = viewportHeight - this.HEADER_HEIGHT;
      this.spacerHeight.set(spacerHeight);
    });
  }

  /**
   * Scrolls to a specific message by ID
   * Call this explicitly when user submits a message
   */
  scrollToMessage(messageId: string): void {
    if (!this.isBrowser) return;

    const element = document.getElementById(`message-${messageId}`);
    if (!element) return;

    // Get the element's position
    const elementRect = element.getBoundingClientRect();
    const absoluteElementTop = elementRect.top + window.scrollY;

    // Calculate offset (header + padding)
    const offset = this.HEADER_HEIGHT + this.SCROLL_PADDING;

    // Scroll to position with offset
    window.scrollTo({
      top: absoluteElementTop - offset,
      behavior: 'smooth'
    });
  }

  /**
   * Scrolls to the last user message
   */
  scrollToLastUserMessage(): void {
    const msgs = this.messages();
    const lastUserMsg = [...msgs].reverse().find(m => m.role === 'user');
    if (lastUserMsg) {
      this.scrollToMessage(lastUserMsg.id);
    }
  }

  /**
   * Debounces a function to limit how often it can be called
   */
  private debounce<T extends (...args: any[]) => any>(
    fn: T,
    delay: number
  ): (...args: Parameters<T>) => void {
    let timeoutId: ReturnType<typeof setTimeout>;
    return (...args: Parameters<T>) => {
      clearTimeout(timeoutId);
      timeoutId = setTimeout(() => fn(...args), delay);
    };
  }
}

