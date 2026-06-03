import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { Message, ContentBlock, ToolUseData } from '../../../services/models/message.model';
import { ToolUseComponent } from './tool-use';
import { ToolRailComponent } from './tool-rail';
import { ToolCallGroup, ToolCallDisplay } from './tool-rail/tool-rail.model';
import { ReasoningContentComponent } from './reasoning-content';
import { StreamingTextComponent } from './streaming-text.component';
import { InlineVisualComponent } from './inline-visual';
import { OAuthConsentPromptComponent } from './oauth-consent-prompt/oauth-consent-prompt.component';
import { McpAppFrameComponent } from './tool-use/renderers/mcp-app-frame.component';
import {
  OAuthConsentRequest,
  OAuthConsentService,
} from '../../../../services/oauth-consent/oauth-consent.service';
import { McpAppStateService } from '../../../services/mcp-apps/mcp-app-state.service';
import type { ToolResultData } from './tool-use/tool-renderer-registry.service';

// ──────────────────────────────────────────────────────────────
// 🔧 MOCK FLAG — set to true to render 10 fake tool calls
//    for visual development. Remove when done.
// ──────────────────────────────────────────────────────────────
const MOCK_TOOL_RAIL = false;

const MOCK_TOOL_GROUP: ToolCallGroup = {
  calls: [
    {
      id: 'mock-1',
      toolName: 'search_knowledge_base',
      input: { query: 'agentcore memory thresholds', top_k: 10 },
      result: { status: 'success', content: [{ text: 'Found 3 relevant documents about memory configuration and retrieval thresholds.' }] },
      status: 'complete',
      durationMs: 1243,
    },
    {
      id: 'mock-2',
      toolName: 'get_session_history',
      input: { session_id: 'sess_abc123', limit: 50 },
      result: { status: 'success', content: [{ json: { messages: 47, turns: 12, last_active: '2026-03-20T09:14:00Z' } }] },
      status: 'complete',
      durationMs: 389,
    },
    {
      id: 'mock-3',
      toolName: 'code_interpreter',
      input: { code: 'import pandas as pd\ndf = pd.read_csv("metrics.csv")\ndf.describe()' },
      result: { status: 'success', content: [{ text: '       count   mean    std     min     25%     50%     75%     max\nlatency  500  124.3   45.2    32.1    94.7   118.6   148.3   312.9\ntokens   500 1847.0  623.1   128.0  1394.0  1812.0  2241.0  4096.0' }] },
      status: 'complete',
      durationMs: 4821,
    },
    {
      id: 'mock-4',
      toolName: 'web_browser',
      input: { url: 'https://docs.aws.amazon.com/bedrock/latest/agentcore/memory-api.html', action: 'read' },
      result: { status: 'success', content: [{ text: 'Amazon Bedrock AgentCore Memory API reference documentation. The RetrievalConfig object supports relevance_score (float 0.0-1.0) and top_k (int 1-1000) parameters for controlling semantic search behavior...' }] },
      status: 'complete',
      durationMs: 2156,
    },
    {
      id: 'mock-5',
      toolName: 'wikipedia_search',
      input: { query: 'vector similarity search thresholds' },
      result: { status: 'error', content: [{ text: 'MCP connection timeout: Gateway did not respond within 30s. Retries exhausted (3/3).' }] },
      status: 'error',
    },
    {
      id: 'mock-6',
      toolName: 'arxiv_search',
      input: { query: 'semantic memory retrieval relevance filtering', max_results: 5 },
      result: { status: 'success', content: [{ json: { papers: [{ title: 'Adaptive Threshold Selection for RAG Systems', year: 2025, arxiv_id: '2501.04832' }, { title: 'Memory-Augmented LLM Agents: A Survey', year: 2025, arxiv_id: '2502.11290' }] } }] },
      status: 'complete',
      durationMs: 1872,
    },
    {
      id: 'mock-7',
      toolName: 'calculate_cost',
      input: { model: 'anthropic.claude-sonnet-4-20250514', input_tokens: 12480, output_tokens: 3200 },
      result: { status: 'success', content: [{ json: { input_cost: 0.0374, output_cost: 0.048, total_cost: 0.0854, currency: 'USD' } }] },
      status: 'complete',
      durationMs: 12,
    },
    {
      id: 'mock-8',
      toolName: 'update_memory_config',
      input: { memory_id: 'mem_xK9f2', namespace: 'facts', relevance_score: 0.7, top_k: 10 },
      status: 'pending',
    },
    {
      id: 'mock-9',
      toolName: 'run_evaluation_suite',
      input: { suite: 'memory_retrieval_quality', dataset: 'golden_qa_v3', threshold: 0.85 },
      status: 'pending',
    },
    {
      id: 'mock-10',
      toolName: 'generate_report',
      input: { format: 'markdown', sections: ['summary', 'recommendations', 'cost_analysis'] },
      status: 'pending',
    },
  ],
};
// ──────────────────────────────────────────────────────────────

/**
 * Display block types for rendering in the template.
 * Transforms content blocks into display-specific blocks that include
 * promoted visuals and grouped tool rails.
 */
interface DisplayBlock {
  type:
    | 'text'
    | 'tool_group'
    | 'tool_use_minimized'
    | 'promoted_visual'
    | 'mcp_app_frame'
    | 'reasoningContent'
    | 'oauth_required';
  data?: ContentBlock;
  // For tool groups (inline rail)
  group?: ToolCallGroup;
  // For promoted visuals
  uiType?: string;
  payload?: unknown;
  toolUseId?: string;
  // For MCP App frames (SEP-1865): the tool result re-shaped for the renderer.
  mcpResult?: ToolResultData;
  // For MCP App frames: whether the REAL tool result has landed. `mcpResult`
  // is a non-null success stub until then, so it can't convey this — the frame
  // needs it as its authoritative "input is final" signal (gates the
  // partial-tool-input relay vs. the complete `tool-input` send).
  inputComplete?: boolean;
  // For MCP App frames: the tool's persisted arguments. On the live path the
  // frame gets the input from the stream parser / captured partial; on reload
  // those are empty, so the frame falls back to this (the input that came back
  // from `GET /messages`) to render the final state instead of a blank canvas.
  toolInput?: Record<string, unknown>;
  // For MCP App frames: the agent-facing tool name, shown in the frame's
  // connected header (the server name + icon come from the `ui_resource`).
  toolName?: string;
  // For inline OAuth consent prompts
  oauthRequest?: OAuthConsentRequest;
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
    OAuthConsentPromptComponent,
    McpAppFrameComponent,
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
          @case ('mcp_app_frame') {
            <div
              class="message-block visual-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <app-mcp-app-frame
                class="block w-full"
                [result]="block.mcpResult!"
                [toolUseId]="block.toolUseId!"
                [inputComplete]="block.inputComplete ?? false"
                [toolInput]="block.toolInput ?? {}"
                [toolName]="block.toolName ?? ''"
              />
            </div>
          }
          @case ('oauth_required') {
            <div
              class="message-block oauth-block"
              [style.animation-delay]="$index * 0.1 + 's'"
            >
              <app-oauth-consent-prompt [request]="block.oauthRequest!" />
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

    /*
     * Entry animation. Uses animation-fill-mode: BACKWARDS (not forwards),
     * and the blocks' resting style is left transform-free.
     *
     * Why not forwards: forwards retains the 100% keyframe after the
     * animation ends, and transform interpolation settles to an identity
     * MATRIX (not the keyword none). Any retained transform — even identity
     * — makes the element a containing block for fixed/absolute descendants.
     * That silently re-anchored the MCP-app fullscreen overlay's
     * position:fixed to the collapsed 0-height message block instead of the
     * viewport (measured: 656x0 vs the expected full 1620x933). backwards
     * applies the 0% frame only during the start delay and then reverts to
     * the resting style, so no transform lingers.
     */
    .message-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) backwards;
      min-width: 0;
    }

    .text-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) backwards;
    }

    .tool-use-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) backwards;
    }

    .reasoning-block {
      animation: slideInFade 0.6s cubic-bezier(0.16, 1, 0.3, 1) backwards;
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

  private consentService = inject(OAuthConsentService);
  private mcpAppState = inject(McpAppStateService);

  /**
   * Transforms content blocks into display blocks.
   * - Consecutive non-promoted tool-use blocks are grouped into a single ToolCallGroup
   *   rendered as an inline rail via app-tool-rail.
   * - Tool-use blocks with promoted visuals (ui_display: "inline") are kept separate
   *   as minimized tool + promoted visual pairs.
   * - Text and reasoning blocks flush any accumulated tool group and stand alone.
   */
  displayBlocks = computed<DisplayBlock[]>(() => {
    // 🔧 MOCK: return fake tool group for visual dev
    if (MOCK_TOOL_RAIL) {
      return [
        { type: 'tool_group', group: MOCK_TOOL_GROUP },
      ];
    }

    const blocks = this.message().content;
    const messageId = this.message().id;
    // Pending interrupts anchored to this message. Used to flip the matching
    // tool_use blocks to ``awaiting_auth`` so the row reads as "paused for
    // authorization" instead of an indefinite spinner.
    const pendingInterruptsHere = this.consentService
      .pending()
      .filter((req) => req.messageId === messageId);
    const hasPendingInterruptHere = pendingInterruptsHere.length > 0;
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
        // An MCP App tool (SEP-1865) renders its sandbox-proxy iframe inside
        // <app-tool-use> via the resultRenderer computed there, so it must
        // escape the collapsed tool_group exactly like a promoted visual.
        // `extractPromotedVisual` only fires on the legacy in-result
        // `ui_type`/`ui_display` marker; MCP Apps deliver UI via a separate
        // `ui_resource` SSE event that arrives *after* `tool_result` and
        // their tool result content carries no inline marker. Reading the
        // signal here keeps `displayBlocks` reactive to a late-arriving
        // `ui_resource` — the computed re-runs when McpAppStateService
        // updates and the tool gets promoted retroactively (vs. staying
        // folded into the group forever).
        const hasMcpAppResource = this.mcpAppState.has(toolUse.toolUseId);

        if (promotedVisual || hasMcpAppResource) {
          // Promoted visuals and MCP Apps both need their own first-class
          // sibling block (the iframe is not "tool output", it's a primary
          // UI surface); break the tool group here.
          flushToolGroup();

          if (promotedVisual) {
            // Legacy promoted visuals still pair a minimized tool card (for
            // provenance) with the visual. MCP Apps don't — the frame renders
            // its own connected header (icon + server + tool + the `</>`
            // request/response toggle), so a separate card would be redundant.
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
          }

          if (hasMcpAppResource) {
            result.push({
              type: 'mcp_app_frame',
              toolUseId: toolUse.toolUseId,
              mcpResult: this.toResultData(toolUse),
              // The REAL result presence — distinct from `mcpResult`'s stub —
              // tells the frame the tool's arguments are done streaming.
              inputComplete: !!toolUse.result,
              // The persisted arguments — the frame's reload fallback when the
              // live stream parser / captured partial are gone.
              toolInput: toolUse.input,
              // Shown in the frame's connected header.
              toolName: toolUse.name,
            });
          }
        } else {
          // Accumulate into the current tool group. A tool_use with no result
          // on a message that has a pending OAuth interrupt is the row that
          // got paused — surface that distinct state instead of a forever-
          // spinning ``pending``.
          const baseStatus = toolUse.status || 'pending';
          const hasNoResult = !toolUse.result;
          const status: ToolCallDisplay['status'] =
            hasPendingInterruptHere && hasNoResult && baseStatus === 'pending'
              ? 'awaiting_auth'
              : baseStatus;
          pendingToolCalls.push({
            id: toolUse.toolUseId,
            toolName: toolUse.name,
            input: toolUse.input || {},
            result: toolUse.result,
            status,
            streamingContent: toolUse.streamingContent,
          });
        }
        continue;
      }
    }

    // Flush any remaining tool calls
    flushToolGroup();

    // Append any pending OAuth consent prompts anchored to this message.
    // Tracking through the consent service signal keeps the synthetic prompt
    // out of message.content so it is never persisted to the backend.
    for (const req of pendingInterruptsHere) {
      result.push({ type: 'oauth_required', oauthRequest: req });
    }

    return result;
  });

  /**
   * Reshape a tool-use's `result` into the renderer's `ToolResultData`
   * contract. Until the `tool_result` event arrives we pass an empty
   * success stub — the renderer holds the iframe until the result comes
   * in (and re-pushes it via the `refreshToolResult` effect).
   */
  private toResultData(toolUse: ToolUseData): ToolResultData {
    return toolUse.result ?? { content: [], status: 'success' };
  }

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
