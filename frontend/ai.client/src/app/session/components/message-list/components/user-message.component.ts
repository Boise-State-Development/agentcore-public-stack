import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { Message } from '../../../services/models/message.model';
@Component({
  selector: 'app-user-message',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="flex w-full justify-end">
      <div
        class="max-w-[80%] rounded-2xl bg-primary-500 px-4 py-3 text-sm/6 text-white"
      >
        @for (block of message().content; track $index) {
          @if (block.type === 'text' && block.text) {
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
export class UserMessageComponent {
  message = input.required<Message>();
}

