import {
    ChangeDetectionStrategy,
    Component,
    computed,
    input,
  } from '@angular/core';

  import { Message } from '../../../services/models/message.model';

  @Component({
    selector: 'app-chat-message',
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
      <div
        class="flex w-full"
        [class.justify-end]="message().role === 'user'"
        [class.justify-start]="message().role === 'assistant'"
      >
        <div
          class="max-w-[80%] rounded-2xl px-4 py-3 text-sm/6"
          [class]="messageClasses()"
        >
          @for (block of message().content; track $index) {
            @if (block.type === 'text') {
              <p class="whitespace-pre-wrap">{{ block.text }}</p>
            }
          }
        </div>
      </div>
    `,
    styles: `
      :host {
        display: block;
      }
    `,
  })
  export class ChatMessageComponent {
    message = input.required<Message>();
  
  messageClasses = computed(() => {
    if (this.message().role === 'user') {
      return 'bg-primary-500 text-white';
    }
    return 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100';
  });
  }