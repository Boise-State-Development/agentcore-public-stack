import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { provideMarkdown, MarkdownService } from 'ngx-markdown';
import { AssistantMessageComponent } from './assistant-message.component';
import { Message, ContentBlock } from '../../../services/models/message.model';
import { McpAppStateService } from '../../../services/mcp-apps/mcp-app-state.service';
import { UiResourceEvent } from '../../../../shared/utils/stream-parser/stream-parser-types';

function makeMessage(content: ContentBlock[]): Message {
  return {
    id: 'msg-1',
    role: 'assistant',
    content,
  };
}

function makeTextBlock(text: string): ContentBlock {
  return { type: 'text', text };
}

function makeToolBlock(name: string, overrides: Record<string, unknown> = {}): ContentBlock {
  return {
    type: 'toolUse',
    toolUse: {
      toolUseId: `tool-${name}-${Math.random().toString(36).slice(2, 6)}`,
      name,
      input: { query: 'test' },
      status: 'complete',
      ...overrides,
    },
  };
}

function makePromotedVisualToolBlock(name: string): ContentBlock {
  return {
    type: 'toolUse',
    toolUse: {
      toolUseId: `tool-${name}`,
      name,
      input: {},
      status: 'complete',
      result: {
        status: 'success',
        content: [{
          json: {
            ui_type: 'chart',
            ui_display: 'inline',
            payload: { data: [1, 2, 3] },
          },
        }],
      },
    },
  };
}

describe('AssistantMessageComponent', () => {
  let fixture: ComponentFixture<AssistantMessageComponent>;
  let component: AssistantMessageComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AssistantMessageComponent],
      providers: [provideMarkdown()],
    }).compileComponents();

    // Stub render before component creation to prevent unhandled
    // rejections from the real KaTeX dependency not being available.
    const markdownService = TestBed.inject(MarkdownService);
    markdownService.render = () => Promise.resolve();

    fixture = TestBed.createComponent(AssistantMessageComponent);
    component = fixture.componentInstance;
  });

  describe('tool grouping logic', () => {
    it('should group a single tool call into one tool_group', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('search_classes'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(1);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(1);
      expect(blocks[0].group!.calls[0].toolName).toBe('search_classes');
    });

    it('should group 3 consecutive tool calls into one tool_group', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('google_drive_search'),
        makeToolBlock('gdrive_fetch'),
        makeToolBlock('web_search'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(1);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(3);
      expect(blocks[0].group!.calls[0].toolName).toBe('google_drive_search');
      expect(blocks[0].group!.calls[1].toolName).toBe('gdrive_fetch');
      expect(blocks[0].group!.calls[2].toolName).toBe('web_search');
    });

    it('should split tool groups when a text block appears between them', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('tool_a'),
        makeToolBlock('tool_b'),
        makeTextBlock('Here are the results:'),
        makeToolBlock('tool_c'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(3);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(2);
      expect(blocks[1].type).toBe('text');
      expect(blocks[2].type).toBe('tool_group');
      expect(blocks[2].group!.calls.length).toBe(1);
    });

    it('should render text blocks standalone', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeTextBlock('Hello world'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(1);
      expect(blocks[0].type).toBe('text');
      expect(blocks[0].data!.text).toBe('Hello world');
    });

    it('should handle text before, between, and after tool groups', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeTextBlock('Let me search for that.'),
        makeToolBlock('search'),
        makeToolBlock('fetch'),
        makeTextBlock('Here is what I found:'),
        makeToolBlock('summarize'),
        makeTextBlock('All done!'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(5);
      expect(blocks[0].type).toBe('text');
      expect(blocks[1].type).toBe('tool_group');
      expect(blocks[1].group!.calls.length).toBe(2);
      expect(blocks[2].type).toBe('text');
      expect(blocks[3].type).toBe('tool_group');
      expect(blocks[3].group!.calls.length).toBe(1);
      expect(blocks[4].type).toBe('text');
    });

    it('should handle empty content array', () => {
      fixture.componentRef.setInput('message', makeMessage([]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(0);
    });
  });

  describe('promoted visuals break tool groups', () => {
    it('should extract promoted visual and render minimized tool + visual', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makePromotedVisualToolBlock('chart_tool'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(2);
      expect(blocks[0].type).toBe('tool_use_minimized');
      expect(blocks[1].type).toBe('promoted_visual');
      expect(blocks[1].uiType).toBe('chart');
    });

    it('should flush pending tool group before a promoted visual', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('search'),
        makeToolBlock('fetch'),
        makePromotedVisualToolBlock('chart_tool'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(3);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(2);
      expect(blocks[1].type).toBe('tool_use_minimized');
      expect(blocks[2].type).toBe('promoted_visual');
    });

    it('should resume grouping regular tools after a promoted visual', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('search'),
        makePromotedVisualToolBlock('chart_tool'),
        makeToolBlock('summarize'),
        makeToolBlock('format'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(4);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(1);
      expect(blocks[1].type).toBe('tool_use_minimized');
      expect(blocks[2].type).toBe('promoted_visual');
      expect(blocks[3].type).toBe('tool_group');
      expect(blocks[3].group!.calls.length).toBe(2);
    });
  });

  // Regression: a tool whose result content carries no inline ui_type/ui_display
  // marker (i.e. not a legacy promoted-visual tool) was being folded into the
  // collapsed tool_group, so no <app-tool-use> ever existed for it — and the
  // MCP App frame (which lives inside <app-tool-use>) never instantiated even
  // though the backend correctly emitted ui_resource. Surfaced dogfooding the
  // excalidraw-mcp server, whose `create_view` returns plain text. The fix
  // promotes any tool whose toolUseId is recorded in McpAppStateService.
  describe('MCP Apps promote tool out of tool_group', () => {
    function makeUiResource(toolUseId: string): UiResourceEvent {
      return {
        type: 'ui_resource',
        toolUseId,
        resourceUri: 'ui://example/app.html',
        html: '<!doctype html><html><body>app</body></html>',
        mimeType: 'text/html;profile=mcp-app',
        csp: {},
        permissions: {},
        sandboxOrigin: 'https://mcp-sandbox.example.com',
      };
    }

    it('folds a plain-text-result tool into tool_group when there is no ui_resource', () => {
      const tool = makeToolBlock('create_view', {
        result: { status: 'success', content: [{ text: 'Diagram displayed!' }] },
      });
      fixture.componentRef.setInput('message', makeMessage([tool]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(1);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(1);
    });

    it('promotes the same tool to a single mcp_app_frame when its toolUseId is in McpAppStateService', () => {
      const mcpAppState = TestBed.inject(McpAppStateService);
      const tool = makeToolBlock('create_view', {
        toolUseId: 'tooluse_mcp_app_1',
        result: { status: 'success', content: [{ text: 'Diagram displayed!' }] },
      });
      mcpAppState.recordLive(makeUiResource('tooluse_mcp_app_1'));

      fixture.componentRef.setInput('message', makeMessage([tool]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      // A single mcp_app_frame block: the frame renders its own connected
      // header (icon + server + tool + the `</>` request/response toggle), so
      // no separate minimized card is emitted. No `promoted_visual` either —
      // MCP Apps have their own type. The tool name rides on the frame block.
      expect(blocks.length).toBe(1);
      expect(blocks[0].type).toBe('mcp_app_frame');
      expect(blocks[0].toolUseId).toBe('tooluse_mcp_app_1');
      expect(blocks[0].toolName).toBe('create_view');
      expect(blocks.some((b) => b.type === 'tool_use_minimized')).toBe(false);
      expect(blocks.some((b) => b.type === 'promoted_visual')).toBe(false);

      mcpAppState.reset();
    });

    it('retroactively promotes when ui_resource arrives AFTER tool_result (late-arrival reactivity)', () => {
      const mcpAppState = TestBed.inject(McpAppStateService);
      const tool = makeToolBlock('create_view', {
        toolUseId: 'tooluse_mcp_app_2',
        result: { status: 'success', content: [{ text: 'Diagram displayed!' }] },
      });

      // Initial render: ui_resource hasn't arrived yet → tool folded into group.
      fixture.componentRef.setInput('message', makeMessage([tool]));
      fixture.detectChanges();
      expect(component.displayBlocks()[0].type).toBe('tool_group');

      // ui_resource arrives ~40ms after tool_result on the wire. The
      // displayBlocks computed must re-run on the McpAppStateService signal
      // update, or the tool stays folded forever.
      mcpAppState.recordLive(makeUiResource('tooluse_mcp_app_2'));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      // Promoted to a single self-headed mcp_app_frame (no minimized sibling).
      expect(blocks.length).toBe(1);
      expect(blocks[0].type).toBe('mcp_app_frame');

      mcpAppState.reset();
    });

    it('promoted-visual tool still emits both tool_use_minimized AND promoted_visual blocks', () => {
      // Sanity: the new gate must not regress the legacy promoted-visual path.
      fixture.componentRef.setInput('message', makeMessage([
        makePromotedVisualToolBlock('chart_tool'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(2);
      expect(blocks[0].type).toBe('tool_use_minimized');
      expect(blocks[1].type).toBe('promoted_visual');
    });
  });

  describe('reasoning content', () => {
    it('should render reasoning blocks and flush tool groups', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('search'),
        {
          type: 'reasoningContent',
          reasoningContent: { reasoningText: { text: 'Thinking...' } },
        },
        makeToolBlock('fetch'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      expect(blocks.length).toBe(3);
      expect(blocks[0].type).toBe('tool_group');
      expect(blocks[0].group!.calls.length).toBe(1);
      expect(blocks[1].type).toBe('reasoningContent');
      expect(blocks[2].type).toBe('tool_group');
      expect(blocks[2].group!.calls.length).toBe(1);
    });
  });

  describe('tool call data mapping', () => {
    it('should map toolUseData fields to ToolCallDisplay correctly', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('my_tool', {
          toolUseId: 'specific-id',
          input: { foo: 'bar' },
          status: 'error',
          result: {
            status: 'error',
            content: [{ text: 'Something went wrong' }],
          },
        }),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      const call = blocks[0].group!.calls[0];
      expect(call.id).toBe('specific-id');
      expect(call.toolName).toBe('my_tool');
      expect(call.input).toEqual({ foo: 'bar' });
      expect(call.status).toBe('error');
      expect(call.result!.status).toBe('error');
      expect(call.result!.content[0].text).toBe('Something went wrong');
    });

    it('should default status to pending when not set', () => {
      fixture.componentRef.setInput('message', makeMessage([
        {
          type: 'toolUse',
          toolUse: {
            toolUseId: 'tool-no-status',
            name: 'running_tool',
            input: {},
            // no status field
          },
        },
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      const call = blocks[0].group!.calls[0];
      expect(call.status).toBe('pending');
    });

    it('should carry streamingContent through to the ToolCallDisplay', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('create_artifact', {
          status: 'pending',
          streamingContent: '<!DOCTYPE html><html><body>partial',
          // no result yet — still generating
          result: undefined,
        }),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      const call = blocks[0].group!.calls[0];
      expect(call.streamingContent).toBe('<!DOCTYPE html><html><body>partial');
      expect(call.status).toBe('pending');
    });

    it('should leave streamingContent undefined for ordinary tool calls', () => {
      fixture.componentRef.setInput('message', makeMessage([
        makeToolBlock('my_tool'),
      ]));
      fixture.detectChanges();

      const blocks = component.displayBlocks();
      const call = blocks[0].group!.calls[0];
      expect(call.streamingContent).toBeUndefined();
    });
  });
});
