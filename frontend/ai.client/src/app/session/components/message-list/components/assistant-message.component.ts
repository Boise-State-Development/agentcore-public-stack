import { ChangeDetectionStrategy, Component, input, computed } from '@angular/core';
import { Message, ContentBlock, ToolUseData } from '../../../services/models/message.model';
import { ToolUseComponent } from './tool-use';
import { ToolRailComponent } from './tool-rail';
import { ToolCallGroup, ToolCallDisplay } from './tool-rail/tool-rail.model';
import { ReasoningContentComponent } from './reasoning-content';
import { StreamingTextComponent } from './streaming-text.component';
import { InlineVisualComponent } from './inline-visual';

/**
 * Display block types for rendering in the template.
 * Transforms content blocks into display-specific blocks that include
 * promoted visuals and grouped tool rails.
 */
interface DisplayBlock {
  type: 'text' | 'tool_group' | 'tool_use_minimized' | 'promoted_visual' | 'reasoningContent';
  data?: ContentBlock;
  // For tool groups (inline rail)
  group?: ToolCallGroup;
  // For promoted visuals
  uiType?: string;
  payload?: unknown;
  toolUseId?: string;
}

@Component({
  selector: 'app-assistant-message',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ToolUseComponent,
    ToolRailComponent,
    ReasoningContentComponent,
    StreamingTextComponent,
    InlineVisualComponent,
  ],
  template: `
    <div class="block-container">
      @for (block of displayBlocks(); track $index) {
        @switch (block.type) {
          @case ('reasoningContent') {
            <div
              class="message-block reasoning-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <app-reasoning-content
                class="flex w-full justify-start"
                [contentBlock]="block.data!"
              />
            </div>
          }
          @case ('text') {
            <div
              class="message-block text-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <div class="flex min-w-0 w-full justify-start">
                <app-streaming-text
                  class="min-w-0 max-w-full overflow-hidden"
                  [text]="block.data!.text!"
                  [isStreaming]="isStreaming()"
                />
              </div>
            </div>
          }
          @case ('tool_group') {
            <div
              class="message-block tool-use-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <app-tool-rail
                class="flex w-full justify-start"
                [group]="block.group!"
              />
            </div>
          }
          @case ('tool_use_minimized') {
            <div
              class="message-block tool-use-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <app-tool-use
                class="flex w-full justify-start"
                [toolUse]="block.data!"
                [minimized]="true"
              />
            </div>
          }
          @case ('promoted_visual') {
            <div
              class="message-block visual-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <app-inline-visual
                [uiType]="block.uiType!"
                [payload]="block.payload"
                [toolUseId]="block.toolUseId!"
              />
            </div>
          }
        }
      }
    </div>
  `,
  styles: `
    @import 'tailwindcss';
    @custom-variant dark (&:where(.dark, .dark *));

    :host {
      display: block;
    }

    .block-container {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      min-width: 0;
    }

    .message-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      opacity: 0;
      transform: translateY(12px);
      min-width: 0;
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
  isStreaming = input<boolean>(false);

  /**
   * Transforms content blocks into display blocks.
   * - Consecutive non-promoted tool-use blocks are grouped into a single ToolCallGroup
   *   rendered as an inline rail via app-tool-rail.
   * - Tool-use blocks with promoted visuals (ui_display: "inline") are kept separate
   *   as minimized tool + promoted visual pairs.
   * - Text and reasoning blocks flush any accumulated tool group and stand alone.
   */
  displayBlocks = computed<DisplayBlock[]>(() => {
    const blocks = this.message().content;
    const result: DisplayBlock[] = [];
    let pendingToolCalls: ToolCallDisplay[] = [];

    const flushToolGroup = () => {
      if (pendingToolCalls.length > 0) {
        result.push({
          type: 'tool_group',
          group: {
            calls: [...pendingToolCalls],
            // groupSummary is not populated yet -- future enhancement.
            // For now, always uses fallback mode (chained tool names).
          },
        });
        pendingToolCalls = [];
      }
    };

    for (const block of blocks) {
      // Handle reasoning content
      if (block.type === 'reasoningContent' && block.reasoningContent) {
        flushToolGroup();
        result.push({ type: 'reasoningContent', data: block });
        continue;
      }

      // Handle text
      if (block.type === 'text' && block.text) {
        flushToolGroup();
        result.push({ type: 'text', data: block });
        continue;
      }

      // Handle tool use
      if ((block.type === 'toolUse' || block.type === 'tool_use') && block.toolUse) {
        const toolUse = block.toolUse as ToolUseData;
        const promotedVisual = this.extractPromotedVisual(toolUse);

        if (promotedVisual) {
          // Promoted visuals break the tool group and render separately
          flushToolGroup();

          result.push({
            type: 'tool_use_minimized',
            data: block,
            toolUseId: toolUse.toolUseId
          });

          result.push({
            type: 'promoted_visual',
            uiType: promotedVisual.uiType,
            payload: promotedVisual.payload,
            toolUseId: toolUse.toolUseId
          });
        } else {
          // Accumulate into the current tool group
          pendingToolCalls.push({
            id: toolUse.toolUseId,
            toolName: toolUse.name,
            input: toolUse.input || {},
            result: toolUse.result,
            status: toolUse.status || 'pending',
          });
        }
        continue;
      }
    }

    // Flush any remaining tool calls
    flushToolGroup();

    return result;
  });

  /**
   * Extract promoted visual data from a tool use result.
   * Returns null if not a promoted visual (no ui_type or ui_display !== 'inline').
   */
  private extractPromotedVisual(toolUse: ToolUseData): { uiType: string; payload: unknown } | null {
    if (!toolUse.result?.content) return null;

    for (const content of toolUse.result.content) {
      // Handle JSON content
      const jsonData = content.json as Record<string, unknown> | undefined
        ?? (content.text ? this.tryParseJson(content.text) : null);

      if (jsonData?.['ui_type'] && jsonData?.['ui_display'] === 'inline') {
        return {
          uiType: jsonData['ui_type'] as string,
          payload: jsonData['payload']
        };
      }
    }

    return null;
  }

  /**
   * Safely parse JSON string, returning null on failure.
   */
  private tryParseJson(text: string): Record<string, unknown> | null {
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }
}
