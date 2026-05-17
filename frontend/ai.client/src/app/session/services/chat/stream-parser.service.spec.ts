// stream-parser.service.spec.ts
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fc from 'fast-check';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';

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
    service.reset();
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
            service.reset();

            // Parse citation event
            service.parseEventSourceMessage('citation', citationData);

            // Get accumulated citations
            const citations = service.citations();

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
            service.reset();

            // Parse citation event without assistantId
            service.parseEventSourceMessage('citation', citationData);

            // Get accumulated citations
            const citations = service.citations();

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

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with missing documentId', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        // documentId missing
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with missing fileName', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        // fileName missing
        text: 'Some text',
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with missing text', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test.pdf',
        // text missing
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string assistantId', () => {
      const malformedCitation = {
        assistantId: 123, // number instead of string
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string documentId', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 123, // number instead of string
        fileName: 'test.pdf',
        text: 'Some text',
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string fileName', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: null, // null instead of string
        text: 'Some text',
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-string text', () => {
      const malformedCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-123',
        fileName: 'test.pdf',
        text: { content: 'Some text' }, // object instead of string
      };

      service.parseEventSourceMessage('citation', malformedCitation);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with null data', () => {
      service.parseEventSourceMessage('citation', null);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with undefined data', () => {
      service.parseEventSourceMessage('citation', undefined);

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should skip citation with non-object data', () => {
      service.parseEventSourceMessage('citation', 'invalid string data');

      const citations = service.citations();
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
          service.parseEventSourceMessage('citation', malformed);
        }).not.toThrow();
      });

      const citations = service.citations();
      expect(citations.length).toBe(0);
    });

    it('should handle valid citation after malformed citation', () => {
      // First, send malformed citation
      const malformedCitation = {
        documentId: 'doc-123',
        // missing assistantId, fileName and text
      };
      service.parseEventSourceMessage('citation', malformedCitation);

      // Then, send valid citation
      const validCitation = {
        assistantId: 'assistant-1',
        documentId: 'doc-456',
        fileName: 'valid.pdf',
        text: 'Valid text',
      };
      service.parseEventSourceMessage('citation', validCitation);

      const citations = service.citations();
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

      service.parseEventSourceMessage('citation', citation);

      const citations = service.citations();
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

      service.parseEventSourceMessage('citation', citation1);
      service.parseEventSourceMessage('citation', citation2);

      const citations = service.citations();
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

      service.parseEventSourceMessage('citation', citation);
      expect(service.citations().length).toBe(1);

      service.reset();
      expect(service.citations().length).toBe(0);
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
    service.reset();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('marks the last turn continuable on a max_tokens stream_error', () => {
    expect(chatState.lastTurnContinuable()).toBe(false);

    service.parseEventSourceMessage('stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'I reached my response-length limit.',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });

    expect(chatState.lastTurnContinuable()).toBe(true);
  });

  it('does not mark continuable for a non-max_tokens stream_error', () => {
    service.parseEventSourceMessage('stream_error', {
      type: 'stream_error',
      code: 'stream_error',
      message: 'Something went wrong.',
      recoverable: false,
    });

    expect(chatState.lastTurnContinuable()).toBe(false);
  });

  it('retires the affordance when the next assistant turn starts streaming', () => {
    service.parseEventSourceMessage('stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'truncated',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });
    expect(chatState.lastTurnContinuable()).toBe(true);

    service.parseEventSourceMessage('message_start', { role: 'assistant' });
    expect(chatState.lastTurnContinuable()).toBe(false);
  });

  it('processes a terminal stream_error even after the stream completed', () => {
    // Reproduces the dropped-affordance bug: the parser reaches a
    // terminal state (message_start sets currentStreamId; done →
    // Completed), then the max_tokens stream_error arrives last. It must
    // still be processed (always-allowed) so Continue appears.
    service.parseEventSourceMessage('message_start', { role: 'assistant' });
    service.parseEventSourceMessage('done', null);
    expect(chatState.lastTurnContinuable()).toBe(false);

    service.parseEventSourceMessage('stream_error', {
      type: 'stream_error',
      code: 'max_tokens',
      message: 'Response length limit reached.',
      recoverable: true,
      metadata: { error_kind: 'max_tokens' },
    });

    expect(chatState.lastTurnContinuable()).toBe(true);
  });
});
