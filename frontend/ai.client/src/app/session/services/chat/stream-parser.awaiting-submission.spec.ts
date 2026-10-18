import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';

/**
 * An MCP App form left open for the user streams as an error result carrying
 * `structuredContent.status = "awaiting_user_submission"`. The parser must
 * keep that field on the tool-use's result, or every renderer reads the
 * result as a failure (dev, 2026-10-18: GitHub's issue_write "Failed").
 */
describe('StreamParserService: a tool result with structuredContent', () => {
  let service: StreamParserService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [StreamParserService, ChatStateService, ErrorService, QuotaWarningService],
    });
    service = TestBed.inject(StreamParserService);
    service.reset('s1');
  });

  afterEach(() => TestBed.resetTestingModule());

  const send = (event: string, data: unknown) =>
    service.parseEventSourceMessage('s1', event, data);

  function streamToolResult(toolResult: Record<string, unknown>) {
    send('message_start', { role: 'assistant' });
    send('content_block_start', {
      contentBlockIndex: 0,
      type: 'tool_use',
      toolUse: { toolUseId: 't1', name: 'issue_write' },
    });
    send('content_block_stop', { contentBlockIndex: 0 });
    send('message_stop', { stopReason: 'tool_use' });
    send('tool_result', { tool_result: { toolUseId: 't1', ...toolResult } });
  }

  const toolUse = () =>
    service
      .allMessagesFor('s1')()
      .flatMap((m) => m.content)
      .find((b) => b.toolUse?.toolUseId === 't1')?.toolUse;

  it('keeps the awaiting marker on the result', () => {
    streamToolResult({
      status: 'error',
      content: [{ text: 'An interactive form has been shown to the user' }],
      structuredContent: { status: 'awaiting_user_submission' },
    });

    expect(toolUse()?.result).toEqual({
      status: 'error',
      content: [{ text: 'An interactive form has been shown to the user' }],
      structuredContent: { status: 'awaiting_user_submission' },
    });
  });

  it('adds no structuredContent key to a result that had none', () => {
    streamToolResult({ status: 'success', content: [{ text: 'ok' }] });

    expect(toolUse()?.result).toEqual({ status: 'success', content: [{ text: 'ok' }] });
  });
});
