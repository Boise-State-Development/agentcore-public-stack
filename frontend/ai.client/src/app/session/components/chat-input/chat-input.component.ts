import { Component, signal, output, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroAdjustmentsHorizontal,
  heroClock,
  heroStop,
  heroPaperAirplane
} from '@ng-icons/heroicons/outline';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ModelDropdownComponent } from '../../../components/model-dropdown/model-dropdown.component';

interface Message {
  content: string;
  timestamp: Date;
}

@Component({
  selector: 'app-chat-input',
  imports: [FormsModule, ModelDropdownComponent, NgIcon],
  providers: [
    provideIcons({
      heroPlus,
      heroAdjustmentsHorizontal,
      heroClock,
      heroStop,
      heroPaperAirplane
    })
  ],
  templateUrl: './chat-input.component.html',
  styleUrl: './chat-input.component.css'
})
export class ChatInputComponent {
  // Service injection
  readonly chatState = inject(ChatStateService);

  // Signals for state management
  userInput = signal('');
  isExpanded = signal(false);
  isFocused = signal(false);
  
  // Output events
  fileAttached = output<File>();
  messageSubmitted = output<Message>();
  messageCancelled = output<void>();

  onSubmit() {
    if(this.chatState.isChatLoading()) {
      this.cancelChatRequest();
    } else {
      this.submitChatRequest();
    }
  }

  submitChatRequest() {
    const content = this.userInput().trim();
    if (content) {
      this.chatState.setChatLoading(true);
      this.messageSubmitted.emit({
        content,
        timestamp: new Date()
      });
      this.userInput.set('');
      this.isExpanded.set(false);
    }
  }

  cancelChatRequest() {
    this.messageCancelled.emit();
  }

  onFileSelect(event: Event) {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files.length > 0) {
      this.fileAttached.emit(input.files[0]);
    }
  }

  onTextareaInput(event: Event) {
    const textarea = event.target as HTMLTextAreaElement;
    this.userInput.set(textarea.value);
    
    // Auto-expand based on content
    textarea.style.height = 'auto';
    textarea.style.height = textarea.scrollHeight + 'px';
  }

  onKeyDown(event: KeyboardEvent) {
    // Submit on Enter (without Shift)
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.onSubmit();
    }
  }

  onFocus() {
    this.isFocused.set(true);
  }

  onBlur() {
    this.isFocused.set(false);
  }
}