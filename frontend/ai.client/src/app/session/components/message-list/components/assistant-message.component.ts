import {
    ChangeDetectionStrategy,
    Component,
    input,
} from '@angular/core';
import { Message } from '../../../services/models/message.model';
import { MarkdownComponent } from 'ngx-markdown';
import { ToolUseComponent } from './tool-use';
import { ReasoningContentComponent } from './reasoning-content';

@Component({
    selector: 'app-assistant-message',
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [MarkdownComponent, ToolUseComponent, ReasoningContentComponent],
    template: `
        <div class="block-container">
            @for (block of message().content; track $index) {
                @if (block.type === 'reasoningContent' && block.reasoningContent) {
                    <div class="message-block reasoning-block" [style.animation-delay]="($index * 0.1) + 's'">
                        <app-reasoning-content class="flex w-full justify-start" [contentBlock]="block"></app-reasoning-content>
                    </div>
                }
                @else if (block.type === 'text' && block.text) {
                    <div class="message-block text-block" [style.animation-delay]="($index * 0.1) + 's'">
                        <div class="flex w-full justify-start">
                            <markdown clipboard mermaid katex [data]="block.text"></markdown>
                        </div>
                    </div>
                }
                @else if ((block.type === 'toolUse' || block.type === 'tool_use') && block.toolUse) {
                    <div class="message-block tool-use-block" [style.animation-delay]="($index * 0.1) + 's'">
                        <app-tool-use class="flex w-full justify-start" [toolUse]="block"></app-tool-use>
                    </div>
                }
            }

        </div>
  `,
    styles: `
    @import "tailwindcss";
    @custom-variant dark (&:where(.dark, .dark *));

    :host {
      display: block;
    }

    .block-container {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }

    .message-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      opacity: 0;
      transform: translateY(12px);
    }

    .text-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }

    .tool-use-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }

    .reasoning-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }

    @keyframes slideInFade {
      0% {
        opacity: 0;
        transform: translateY(12px) scale(0.98);
      }
      100% {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `,
})
export class AssistantMessageComponent {
    message = input.required<Message>();
}

