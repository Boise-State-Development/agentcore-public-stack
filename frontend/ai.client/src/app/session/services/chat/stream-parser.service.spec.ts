// stream-parser.service.spec.ts
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fc from 'fast-check';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';
import { SessionService } from '../session/session.service';

describe('StreamParserService - Citation Handling', () => {
  let service: StreamParserService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        StreamParserService,
        ChatStateService,
        ErrorService,
        QuotaWarningService,
      ],
    });
    service = TestBed.inject(StreamParserService);
    service.reset('s1');
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  // =========================================================================
  // Property-Based Tests
  // =========================================================================

  describe('Property Tests', () => {
    // Feature: rag-citation-display, Property: Citation fields are correctly mapped
    it('should correctly map all citation fields for any valid citation event', () => {
      fc.assert(
        fc.property(
          fc.record({
            assistantId: fc.string({ minLength: 1 }),
            documentId: fc.string({ minLength: 1 }),
            fileName: fc.string({ minLength: 1 }),
            text: fc.string({ minLength: 1 }),
          }),
          (citationData: { assistantId: string; documentId: string; fileName: string; text: string }) => {
            // Reset service for each iteration
            service.reset('s1');

            // Parse citation event
            service.parseEventSourceMessage('s1', 'citation', citationData);

            // Get accumulated citations
            const citations = service.citationsFor('s1')();

            // Verify citation was added
            expect(citations.length).toBe(1);

            // Verify all fields were correctly mapped
            const citation = citations[0];
            expect(citation.assistantId).toBe(citationData.assistantId);
            expect(citation.documentId).toBe(citationData.documentId);
            expect(citation.fileName).toBe(citationData.fileName);
            expect(citation.text).toBe(citationData.text);
          }
        ),
        { numRuns: 100 }
      );
    });

    // Feature: rag-citation-display, Property: Missing required fields cause rejection
    it('should not add citation when required fields are missing', () => {
      fc.assert(
        fc.property(
          fc.record({
            documentId: fc.string({ minLength: 1 }),
            fileName: fc.string({ minLength: 1 }),
            text: fc.string({ minLength: 1 }),
            // assistantId intentionally missing
          }),
          (citationData: { documentId: string; fileName: string; text: string }) => {
            // Reset service for each iteration
            service.reset('s1');

            // Parse citation event without assistantId
            service.parseEventSourceMessage('s1', 'citation', citationData);

            // Get accumulated citations
            const citations = service.citationsFor('s1')();

            // Verify citation was NOT added (missing assistantId)
            expect(citations.length).toBe(0);
          }
        ),
        { numRuns: 100 }
      );
    });
  });

  // =========================================================================
  // Unit Tests - Malformed Citation Handling
  // =========================================================================

  describe('Unit Tests - Malformed Citation Handling', () => {
    it('should skip citation with missing assistantId', () => {
      const malformedCitation = {
        // assistantId missing
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with missing documentId', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        // documentId missing
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with missing fileName', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        // fileName missing
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with missing text', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test.pdf',
        // text missing
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string assistantId', () => {
      const malformedCitation = {
        assistantId: 123, // number instead of string
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string documentId', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 123, // number instead of string
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string fileName', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: null, // null instead of string
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string text', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: { content: 'Some text' }, // object instead of string
      };

      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with null data', () => {
      service.parseEventSourceMessage('s1', 'citation', null);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with undefined data', () => {
      service.parseEventSourceMessage('s1', 'citation', undefined);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-object data', () => {
      service.parseEventSourceMessage('s1', 'citation', 'invalid string data');

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should not throw error when processing malformed citations', () => {
      const malformedCitations = [
        null,
        undefined,
        'string',
        123,
        [],
        { documentId: 'doc-123' }, // missing required fields
        { fileName: 'test.pdf' }, // missing required fields
        { text: 'Some text' }, // missing required fields
        { assistantId: 'assistant-1' }, // missing required fields
      ];

      malformedCitations.forEach((malformed) => {
        expect(() => {
          service.parseEventSourceMessage('s1', 'citation', malformed);
        }).not.toThrow();
      });

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(0);
    });

    it('should handle valid citation after malformed citation', () => {
      // First, send malformed citation
      const malformedCitation = {
        documentId: 'doc-123',
        // missing assistantId, fileName and text
      };
      service.parseEventSourceMessage('s1', 'citation', malformedCitation);

      // Then, send valid citation
      const validCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-456',
        fileName: 'valid.pdf',
        text: 'Valid text',
      };
      service.parseEventSourceMessage('s1', 'citation', validCitation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(1);
      expect(citations[0].assistantId).toBe('assistant-1');
      expect(citations[0].documentId).toBe('doc-456');
      expect(citations[0].fileName).toBe('valid.pdf');
      expect(citations[0].text).toBe('Valid text');
    });
  });

  // =========================================================================
  // Unit Tests - Valid Citation Handling
  // =========================================================================

  describe('Unit Tests - Valid Citation Handling', () => {
    it('should handle valid citation with all required fields', () => {
      const citation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: 'Some relevant text from the document',
      };

      service.parseEventSourceMessage('s1', 'citation', citation);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(1);
      expect(citations[0].assistantId).toBe('assistant-1');
      expect(citations[0].documentId).toBe('doc-123');
      expect(citations[0].fileName).toBe('test.pdf');
      expect(citations[0].text).toBe('Some relevant text from the document');
    });

    it('should accumulate multiple citations', () => {
      const citation1 = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test1.pdf',
        text: 'Text from first document',
      };

      const citation2 = {
        assistantId: 'assistant-1',
        documentId: 'doc-456',
        fileName: 'test2.pdf',
        text: 'Text from second document',
      };

      service.parseEventSourceMessage('s1', 'citation', citation1);
      service.parseEventSourceMessage('s1', 'citation', citation2);

      const citations = service.citationsFor('s1')();
      expect(citations.length).toBe(2);
      expect(citations[0].documentId).toBe('doc-123');
      expect(citations[0].assistantId).toBe('assistant-1');
      expect(citations[1].documentId).toBe('doc-456');
      expect(citations[1].assistantId).toBe('assistant-1');
    });

    it('should clear citations on reset', () => {
      const citation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('s1', 'citation', citation);
      expect(service.citationsFor('s1')().length).toBe(1);

      service.reset('s1');
      expect(service.citationsFor('s1')().length).toBe(0);
    });
  });
});

describe('StreamParserService - max_tokens Continue affordance', () => {
  let service: StreamParserService;
  let chatState: ChatStateService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        StreamParserService,
        ChatStateService,
        ErrorService,
        QuotaWarningService,
      ],
    });
    service = TestBed.inject(StreamParserService);
    chatState = TestBed.inject(ChatStateService);
    // The Continue affordance reads through the viewed-session facade.
    chatState.setViewedSession('s1');
    service.reset('s1');
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('marks the last turn continuable on a max_tokens stream_error', () => {
    expect(chatState.lastTurnContinuable()).toBe(false);

    service.parseEventSourceMessage('s1', 'stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'I reached my response-length limit.',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });

    expect(chatState.lastTurnContinuable()).toBe(true);
  });

  it('does not mark continuable for a non-max_tokens stream_error', () => {
    service.parseEventSourceMessage('s1', 'stream_error', {
      type: 'stream_error',
      code: 'stream_error',
      message: 'Something went wrong.',
      recoverable: false,
    });

    expect(chatState.lastTurnContinuable()).toBe(false);
  });

  it('retires the affordance when the next assistant turn starts streaming', () => {
    service.parseEventSourceMessage('s1', 'stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'truncated',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });
    expect(chatState.lastTurnContinuable()).toBe(true);

    service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' });
    expect(chatState.lastTurnContinuable()).toBe(false);
  });

  it('processes a terminal stream_error even after the stream completed', () => {
    // Reproduces the dropped-affordance bug: the parser reaches a
    // terminal state (message_start sets currentStreamId; done →
    // Completed), then the max_tokens stream_error arrives last. It must
    // still be processed (always-allowed) so Continue appears.
    service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' });
    service.parseEventSourceMessage('s1', 'done', null);
    expect(chatState.lastTurnContinuable()).toBe(false);

    service.parseEventSourceMessage('s1', 'stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'Response length limit reached.',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });

    expect(chatState.lastTurnContinuable()).toBe(true);
  });

  it('keys the affordance to the stream session, not the viewed one', () => {
    // A background stream (session s2) truncating must not surface
    // Continue on the viewed conversation (s1).
    service.reset('s2');
    service.parseEventSourceMessage('s2', 'stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'truncated',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });

    expect(chatState.lastTurnContinuable()).toBe(false);

    chatState.setViewedSession('s2');
    expect(chatState.lastTurnContinuable()).toBe(true);
  });
});

describe('StreamParserService - concurrent session isolation', () => {
  let service: StreamParserService;

  const streamText = (sessionId: string, text: string) => {
    service.parseEventSourceMessage(sessionId, 'message_start', { role: 'assistant' });
    service.parseEventSourceMessage(sessionId, 'content_block_delta', {
      contentBlockIndex: 0,
      text,
    });
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        StreamParserService,
        ChatStateService,
        ErrorService,
        QuotaWarningService,
      ],
    });
    service = TestBed.inject(StreamParserService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('parses two interleaved streams into separate per-session state', () => {
    service.reset('a', 1);
    service.reset('b', 1);

    streamText('a', 'alpha');
    streamText('b', 'bravo');
    service.parseEventSourceMessage('a', 'content_block_delta', {
      contentBlockIndex: 0,
      text: '-more',
    });

    const aMessages = service.allMessagesFor('a')();
    const bMessages = service.allMessagesFor('b')();

    expect(aMessages).toHaveLength(1);
    expect(aMessages[0].id).toBe('msg-a-1');
    expect(aMessages[0].content).toEqual([{ type: 'text', text: 'alpha-more' }]);

    expect(bMessages).toHaveLength(1);
    expect(bMessages[0].id).toBe('msg-b-1');
    expect(bMessages[0].content).toEqual([{ type: 'text', text: 'bravo' }]);
  });

  it('keeps session A streaming after session B resets (route change / new stream)', () => {
    service.reset('a');
    streamText('a', 'hello');

    // Session B starting a stream must not disturb A's in-flight parse.
    service.reset('b');
    streamText('b', 'other');

    service.parseEventSourceMessage('a', 'content_block_delta', {
      contentBlockIndex: 0,
      text: ' world',
    });

    expect(service.allMessagesFor('a')()[0].content).toEqual([
      { type: 'text', text: 'hello world' },
    ]);
    expect(service.streamingMessageIdFor('a')()).toBe('msg-a-0');
  });

  it('drops late events carrying a superseded stream id (same-session resubmit)', () => {
    service.reset('a');
    const staleStreamId = service.getCurrentStreamId('a');
    streamText('a', 'first attempt');

    // A re-submit resets the session's parser; the old stream's late
    // events identify themselves with the captured (now stale) stream id.
    service.reset('a');
    expect(service.getCurrentStreamId('a')).not.toBe(staleStreamId);

    service.parseEventSourceMessage(
      'a',
      'message_start',
      { role: 'assistant' },
      staleStreamId,
    );
    service.parseEventSourceMessage(
      'a',
      'content_block_delta',
      { contentBlockIndex: 0, text: 'stale text' },
      staleStreamId,
    );

    expect(service.allMessagesFor('a')()).toHaveLength(0);

    // The replacement stream still parses normally with its own id.
    const freshStreamId = service.getCurrentStreamId('a');
    service.parseEventSourceMessage('a', 'message_start', { role: 'assistant' }, freshStreamId);
    service.parseEventSourceMessage(
      'a',
      'content_block_delta',
      { contentBlockIndex: 0, text: 'fresh text' },
      freshStreamId,
    );
    expect(service.allMessagesFor('a')()[0].content).toEqual([
      { type: 'text', text: 'fresh text' },
    ]);
  });

  it('ignores events for a session with no parser state', () => {
    expect(() =>
      service.parseEventSourceMessage('ghost', 'message_start', { role: 'assistant' }),
    ).not.toThrow();
    expect(service.allMessagesFor('ghost')()).toEqual([]);
  });

  it('routes cost metadata to the stream session in ChatStateService', () => {
    const chatState = TestBed.inject(ChatStateService);
    chatState.setViewedSession('a');
    service.reset('a');
    service.reset('b');

    // Background session B finishes a turn while A is viewed.
    service.parseEventSourceMessage('b', 'message_start', { role: 'assistant' });
    service.parseEventSourceMessage('b', 'metadata', {
      usage: { inputTokens: 100, outputTokens: 50, totalTokens: 150 },
      cost: 0.25,
      contextWindow: 200000,
    });

    // Viewed badge (session A) is untouched...
    expect(chatState.costDollars()).toBe(0);
    // ...and B's own aggregates carry the turn.
    chatState.setViewedSession('b');
    expect(chatState.costDollars()).toBe(0.25);
    expect(chatState.contextTokens()).toBe(100);
  });
});

describe('StreamParserService - session_title events', () => {
  let service: StreamParserService;
  let applyServerTitle: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    applyServerTitle = vi.fn();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        StreamParserService,
        ChatStateService,
        ErrorService,
        QuotaWarningService,
        { provide: SessionService, useValue: { applyServerTitle } },
      ],
    });
    service = TestBed.inject(StreamParserService);
    service.reset('s1');
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('applies a mid-stream session_title to the session cache and header', () => {
    service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' });
    service.parseEventSourceMessage('s1', 'session_title', {
      type: 'session_title',
      sessionId: 's1',
      title: 'Python CSV Parser Script',
    });

    expect(applyServerTitle).toHaveBeenCalledWith('s1', 'Python CSV Parser Script');
  });

  it('still applies a session_title arriving after done (late generation)', () => {
    service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' });
    service.parseEventSourceMessage('s1', 'done', null);
    service.parseEventSourceMessage('s1', 'session_title', {
      type: 'session_title',
      sessionId: 's1',
      title: 'Late Title',
    });

    expect(applyServerTitle).toHaveBeenCalledWith('s1', 'Late Title');
  });

  it('ignores a session_title whose sessionId does not match the stream', () => {
    service.parseEventSourceMessage('s1', 'session_title', {
      type: 'session_title',
      sessionId: 'other-session',
      title: 'Wrong Stream',
    });

    expect(applyServerTitle).not.toHaveBeenCalled();
  });

  it('drops an invalid session_title without applying anything', () => {
    service.parseEventSourceMessage('s1', 'session_title', {
      type: 'session_title',
      sessionId: 's1',
      title: '',
    });

    expect(applyServerTitle).not.toHaveBeenCalled();
  });
});
