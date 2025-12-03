import {
    ChangeDetectionStrategy,
    Component,
    input,
} from '@angular/core';
import { Message } from '../../../services/models/message.model';
import { MarkdownComponent } from 'ngx-markdown';
import { ToolUseComponent } from './tool-use';

@Component({
    selector: 'app-assistant-message',
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [MarkdownComponent, ToolUseComponent],
    template: `
        <div>
            @for (block of message().content; track $index) {
                @if (block.type === 'text') {
                    <div class="flex w-full justify-start">
                        <markdown clipboard mermaid katex [data]="block.text"></markdown>
                    </div>
                }
                @else if (block.type === 'tool_use') {
                    <div >
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
  `,
})
export class AssistantMessageComponent {
    message = input.required<Message>();
}

