import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { provideMarkdown, MarkdownService } from 'ngx-markdown';
import { AssistantMessageComponent } from './assistant-message.component';
import { Message, ContentBlock } from '../../../services/models/message.model';

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
  });
});
