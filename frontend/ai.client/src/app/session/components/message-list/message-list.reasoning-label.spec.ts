import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import type { ContentBlock, Message } from '../../services/models/message.model';
import type { AgentStatusEvent } from '../../../shared/utils/stream-parser';

/**
 * "Reasoning" on the loading line while a reasoning summary streams.
 *
 * With summarized thinking on Opus 5.5 and Haiku 5.5, the Thinking block shows
 * the model's reasoning while the loading line under it used to say "Waiting
 * for the model" (seen on a local stack against dev). The label now follows
 * the content stream: reasoning only while the newest message ENDS in an open
 * reasoning span.
 */
describe('MessageListComponent — reasoning label', () => {
  let fixture: ComponentFixture<MessageListComponent>;
  let insights: ToolInsightService;

  const SESSION = 'sess-reasoning';

  const reasoning = (text: string, durationMs?: number): ContentBlock =>
    ({
      type: 'reasoningContent',
      reasoningContent: { reasoningText: { text } },
      ...(durationMs === undefined ? {} : { reasoningDurationMs: durationMs }),
    }) as ContentBlock;

  const turn = (...content: ContentBlock[]): Message[] => [
    { id: 'u1', role: 'user', content: [{ type: 'text', text: 'Work it out.' }] },
    { id: 'a1', role: 'assistant', content },
  ];

  /**
   * Deliberately NO `detectChanges`, as in `message-list.turn-recap.spec.ts`:
   * the label is a computed over signals and needs no render, and rendering
   * an assistant message pulls ngx-markdown into a KaTeX dependency whose
   * async render rejects after teardown, which fails the CI run.
   */
  function label(messages: Message[], phase?: AgentStatusEvent['phase']): string | null {
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', messages);
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.componentRef.setInput('sessionId', SESSION);
    if (phase) {
      insights.recordStatus(SESSION, {
        type: 'agent_status',
        sessionId: SESSION,
        phase,
        cycle: 1,
      } as AgentStatusEvent);
    }
    return (
      fixture.componentInstance as unknown as { loaderStatus: () => string | null }
    ).loaderStatus();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    TestBed.configureTestingModule({
      imports: [MessageListComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideMarkdown(),
      ],
    });
    insights = TestBed.inject(ToolInsightService);
    TestBed.inject(ChatStateService).setViewedSession(SESSION);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  it('says Reasoning while the summary streams, not Waiting for the model', () => {
    expect(label(turn(reasoning('Setting up the recurrence. ')), 'thinking')).toBe('Reasoning');
  });

  it('says Reasoning without a status frame too, since it reads the content stream', () => {
    expect(label(turn(reasoning('Setting up the recurrence. ')))).toBe('Reasoning');
  });

  it('lets go once the span has closed', () => {
    expect(label(turn(reasoning('Done.', 4_000)), 'thinking')).toBe('Waiting for the model');
  });

  it('lets go once the answer starts', () => {
    expect(label(turn(reasoning('Done.'), { type: 'text', text: 'The answer' } as ContentBlock))).toBe(
      'Thinking',
    );
  });

  it('lets go once the model moves on to a tool call', () => {
    const toolUse = {
      type: 'toolUse',
      toolUse: { toolUseId: 't1', name: 'calculator', input: {} },
    } as ContentBlock;
    // The unanswered call reads as running through the content-stream fallback.
    expect(label(turn(reasoning('Need a number.'), toolUse), 'thinking')).toBe('Running');
  });

  it('ignores a signature-only block, which has nothing to show', () => {
    expect(label(turn(reasoning('')), 'thinking')).toBe('Waiting for the model');
  });
});
