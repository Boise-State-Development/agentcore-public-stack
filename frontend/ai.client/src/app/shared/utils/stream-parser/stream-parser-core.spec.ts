import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  validateMessageStartEvent,
  validateContentBlockStartEvent,
  validateContentBlockDeltaEvent,
  validateContentBlockStopEvent,
  validateMessageStopEvent,
  validateToolUseEvent,
  validateToolResultEvent,
  validateQuotaWarningEvent,
  validateQuotaExceededEvent,
  validateConversationalStreamError,
  validateCitation,
  validateArtifactEvent,
  validateUiResourceEvent,
  validateToolInputPartialEvent,
  processStreamEvent,
  createStreamLineParser,
  inferContentBlockType,
  extractStreamingStringField,
  parseToolResultContent,
  StreamParserCallbacks
} from './stream-parser-core';

describe('stream-parser-core', () => {
  describe('validateMessageStartEvent', () => {
    it('should return true for valid user message', () => {
      expect(validateMessageStartEvent({ role: 'user' })).toBe(true);
    });

    it('should return true for valid assistant message', () => {
      expect(validateMessageStartEvent({ role: 'assistant' })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateMessageStartEvent(null)).toBe(false);
      expect(validateMessageStartEvent(undefined)).toBe(false);
    });

    it('should return false for non-object', () => {
      expect(validateMessageStartEvent('string')).toBe(false);
      expect(validateMessageStartEvent(123)).toBe(false);
    });

    it('should return false for invalid role', () => {
      expect(validateMessageStartEvent({ role: 'invalid' })).toBe(false);
      expect(validateMessageStartEvent({})).toBe(false);
    });
  });

  describe('validateContentBlockStartEvent', () => {
    it('should return true for valid event with contentBlockIndex', () => {
      expect(validateContentBlockStartEvent({ contentBlockIndex: 0 })).toBe(true);
      expect(validateContentBlockStartEvent({ contentBlockIndex: 1 })).toBe(true);
    });

    it('should return true for valid event with type', () => {
      expect(validateContentBlockStartEvent({ contentBlockIndex: 0, type: 'text' })).toBe(true);
      expect(validateContentBlockStartEvent({ contentBlockIndex: 0, type: 'tool_use' })).toBe(true);
    });

    it('should return true for tool_use with toolUse', () => {
      expect(validateContentBlockStartEvent({
        contentBlockIndex: 0,
        type: 'tool_use',
        toolUse: { toolUseId: 'id', name: 'tool' }
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateContentBlockStartEvent(null)).toBe(false);
      expect(validateContentBlockStartEvent(undefined)).toBe(false);
    });

    it('should return false for missing contentBlockIndex', () => {
      expect(validateContentBlockStartEvent({})).toBe(false);
      expect(validateContentBlockStartEvent({ type: 'text' })).toBe(false);
    });

    it('should return false for invalid contentBlockIndex', () => {
      expect(validateContentBlockStartEvent({ contentBlockIndex: -1 })).toBe(false);
      expect(validateContentBlockStartEvent({ contentBlockIndex: 1.5 })).toBe(false);
      expect(validateContentBlockStartEvent({ contentBlockIndex: 'string' })).toBe(false);
    });

    it('should return false for invalid type', () => {
      expect(validateContentBlockStartEvent({ contentBlockIndex: 0, type: 'invalid' })).toBe(false);
    });

    it('should return false for tool_use without valid toolUse', () => {
      expect(validateContentBlockStartEvent({
        contentBlockIndex: 0,
        type: 'tool_use',
        toolUse: { name: 'tool' }
      })).toBe(false);
    });
  });

  describe('validateContentBlockDeltaEvent', () => {
    it('should return true for valid text delta', () => {
      expect(validateContentBlockDeltaEvent({ contentBlockIndex: 0, text: 'hello' })).toBe(true);
    });

    it('should return true for valid tool_use delta', () => {
      expect(validateContentBlockDeltaEvent({ contentBlockIndex: 0, input: '{}' })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateContentBlockDeltaEvent(null)).toBe(false);
      expect(validateContentBlockDeltaEvent(undefined)).toBe(false);
    });

    it('should return false for missing contentBlockIndex', () => {
      expect(validateContentBlockDeltaEvent({ text: 'hello' })).toBe(false);
    });

    it('should return false for invalid contentBlockIndex', () => {
      expect(validateContentBlockDeltaEvent({ contentBlockIndex: -1, text: 'hello' })).toBe(false);
    });

    it('should return false for missing text and input', () => {
      expect(validateContentBlockDeltaEvent({ contentBlockIndex: 0 })).toBe(false);
    });

    it('should return false for invalid type', () => {
      expect(validateContentBlockDeltaEvent({ contentBlockIndex: 0, type: 'invalid', text: 'hello' })).toBe(false);
    });
  });

  describe('validateContentBlockStopEvent', () => {
    it('should return true for valid event', () => {
      expect(validateContentBlockStopEvent({ contentBlockIndex: 0 })).toBe(true);
      expect(validateContentBlockStopEvent({ contentBlockIndex: 5 })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateContentBlockStopEvent(null)).toBe(false);
      expect(validateContentBlockStopEvent(undefined)).toBe(false);
    });

    it('should return false for missing contentBlockIndex', () => {
      expect(validateContentBlockStopEvent({})).toBe(false);
    });

    it('should return false for invalid contentBlockIndex', () => {
      expect(validateContentBlockStopEvent({ contentBlockIndex: -1 })).toBe(false);
      expect(validateContentBlockStopEvent({ contentBlockIndex: 1.5 })).toBe(false);
    });
  });

  describe('validateMessageStopEvent', () => {
    it('should return true for valid event', () => {
      expect(validateMessageStopEvent({ stopReason: 'end_turn' })).toBe(true);
      expect(validateMessageStopEvent({ stopReason: 'max_tokens' })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateMessageStopEvent(null)).toBe(false);
      expect(validateMessageStopEvent(undefined)).toBe(false);
    });

    it('should return false for missing stopReason', () => {
      expect(validateMessageStopEvent({})).toBe(false);
    });

    it('should return false for empty stopReason', () => {
      expect(validateMessageStopEvent({ stopReason: '' })).toBe(false);
    });

    it('should return false for non-string stopReason', () => {
      expect(validateMessageStopEvent({ stopReason: 123 })).toBe(false);
    });
  });

  describe('validateToolUseEvent', () => {
    it('should return true for valid event', () => {
      expect(validateToolUseEvent({
        tool_use: { name: 'search', tool_use_id: 'id123' }
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateToolUseEvent(null)).toBe(false);
      expect(validateToolUseEvent(undefined)).toBe(false);
    });

    it('should return false for missing tool_use', () => {
      expect(validateToolUseEvent({})).toBe(false);
    });

    it('should return false for invalid tool_use', () => {
      expect(validateToolUseEvent({ tool_use: 'string' })).toBe(false);
    });

    it('should return false for missing name or tool_use_id', () => {
      expect(validateToolUseEvent({ tool_use: { name: 'search' } })).toBe(false);
      expect(validateToolUseEvent({ tool_use: { tool_use_id: 'id' } })).toBe(false);
    });

    it('should return false for empty name or tool_use_id', () => {
      expect(validateToolUseEvent({ tool_use: { name: '', tool_use_id: 'id' } })).toBe(false);
      expect(validateToolUseEvent({ tool_use: { name: 'search', tool_use_id: '' } })).toBe(false);
    });
  });

  describe('validateToolResultEvent', () => {
    it('should return true for valid event', () => {
      expect(validateToolResultEvent({
        tool_result: { toolUseId: 'id123' }
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateToolResultEvent(null)).toBe(false);
      expect(validateToolResultEvent(undefined)).toBe(false);
    });

    it('should return false for missing tool_result', () => {
      expect(validateToolResultEvent({})).toBe(false);
    });

    it('should return false for invalid tool_result', () => {
      expect(validateToolResultEvent({ tool_result: 'string' })).toBe(false);
    });

    it('should return false for missing toolUseId', () => {
      expect(validateToolResultEvent({ tool_result: {} })).toBe(false);
    });

    it('should return false for empty toolUseId', () => {
      expect(validateToolResultEvent({ tool_result: { toolUseId: '' } })).toBe(false);
    });
  });

  describe('validateQuotaWarningEvent', () => {
    it('should return true for valid event', () => {
      expect(validateQuotaWarningEvent({
        type: 'quota_warning',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateQuotaWarningEvent(null)).toBe(false);
      expect(validateQuotaWarningEvent(undefined)).toBe(false);
    });

    it('should return false for wrong type', () => {
      expect(validateQuotaWarningEvent({
        type: 'wrong',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80
      })).toBe(false);
    });

    it('should return false for missing required fields', () => {
      expect(validateQuotaWarningEvent({ type: 'quota_warning' })).toBe(false);
      expect(validateQuotaWarningEvent({
        type: 'quota_warning',
        currentUsage: 8
      })).toBe(false);
    });

    it('should return false for non-number fields', () => {
      expect(validateQuotaWarningEvent({
        type: 'quota_warning',
        currentUsage: 'string',
        quotaLimit: 10,
        percentageUsed: 80
      })).toBe(false);
    });
  });

  describe('validateQuotaExceededEvent', () => {
    it('should return true for valid event', () => {
      expect(validateQuotaExceededEvent({
        type: 'quota_exceeded',
        currentUsage: 12,
        quotaLimit: 10,
        percentageUsed: 120
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateQuotaExceededEvent(null)).toBe(false);
      expect(validateQuotaExceededEvent(undefined)).toBe(false);
    });

    it('should return false for wrong type', () => {
      expect(validateQuotaExceededEvent({
        type: 'wrong',
        currentUsage: 12,
        quotaLimit: 10,
        percentageUsed: 120
      })).toBe(false);
    });

    it('should return false for missing required fields', () => {
      expect(validateQuotaExceededEvent({ type: 'quota_exceeded' })).toBe(false);
    });
  });

  describe('validateConversationalStreamError', () => {
    it('should return true for valid event', () => {
      expect(validateConversationalStreamError({
        type: 'stream_error',
        code: 'ERROR_CODE',
        message: 'Error message',
        recoverable: true
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateConversationalStreamError(null)).toBe(false);
      expect(validateConversationalStreamError(undefined)).toBe(false);
    });

    it('should return false for wrong type', () => {
      expect(validateConversationalStreamError({
        type: 'wrong',
        code: 'ERROR_CODE',
        message: 'Error message',
        recoverable: true
      })).toBe(false);
    });

    it('should return false for missing required fields', () => {
      expect(validateConversationalStreamError({ type: 'stream_error' })).toBe(false);
    });

    it('should return false for non-boolean recoverable', () => {
      expect(validateConversationalStreamError({
        type: 'stream_error',
        code: 'ERROR_CODE',
        message: 'Error message',
        recoverable: 'true'
      })).toBe(false);
    });
  });

  describe('validateCitation', () => {
    it('should return true for valid citation', () => {
      expect(validateCitation({
        assistantId: 'assistant1',
        documentId: 'doc1',
        fileName: 'file.txt',
        text: 'citation text'
      })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateCitation(null)).toBe(false);
      expect(validateCitation(undefined)).toBe(false);
    });

    it('should return false for missing required fields', () => {
      expect(validateCitation({ assistantId: 'assistant1' })).toBe(false);
      expect(validateCitation({
        assistantId: 'assistant1',
        documentId: 'doc1'
      })).toBe(false);
    });

    it('should return false for non-string fields', () => {
      expect(validateCitation({
        assistantId: 123,
        documentId: 'doc1',
        fileName: 'file.txt',
        text: 'citation text'
      })).toBe(false);
    });
  });

  describe('validateArtifactEvent', () => {
    const valid = {
      type: 'artifact',
      artifactId: 'art-1',
      version: 1,
      title: 'Sales Dashboard',
      contentType: 'text/html; charset=utf-8',
      sessionId: 'sess-9',
      updatedAt: '2026-05-15T12:00:05+00:00',
      action: 'created'
    };

    it('should return true for a valid created artifact', () => {
      expect(validateArtifactEvent(valid)).toBe(true);
    });

    it('should return true for an updated artifact (version > 1)', () => {
      expect(validateArtifactEvent({ ...valid, version: 4, action: 'updated' })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateArtifactEvent(null)).toBe(false);
      expect(validateArtifactEvent(undefined)).toBe(false);
    });

    it('should return false when type is not "artifact"', () => {
      expect(validateArtifactEvent({ ...valid, type: 'compaction' })).toBe(false);
    });

    it('should return false for empty artifactId', () => {
      expect(validateArtifactEvent({ ...valid, artifactId: '' })).toBe(false);
    });

    it('should return false for version < 1 or non-integer', () => {
      expect(validateArtifactEvent({ ...valid, version: 0 })).toBe(false);
      expect(validateArtifactEvent({ ...valid, version: 1.5 })).toBe(false);
    });

    it('should return false for an unknown action', () => {
      expect(validateArtifactEvent({ ...valid, action: 'deleted' })).toBe(false);
    });

    it('should return false for missing fields', () => {
      expect(validateArtifactEvent({ type: 'artifact', artifactId: 'art-1' })).toBe(false);
    });
  });

  describe('validateUiResourceEvent', () => {
    const valid = {
      type: 'ui_resource',
      toolUseId: 'tu-1',
      resourceUri: 'ui://srv/widget',
      html: '<h1>hi</h1>',
      mimeType: 'text/html;profile=mcp-app',
      csp: { connectDomains: ['https://api.test'] },
      permissions: { clipboardWrite: {} },
      sandboxOrigin: 'https://mcp-sandbox.example.com'
    };

    it('should return true for a valid ui_resource event', () => {
      expect(validateUiResourceEvent(valid)).toBe(true);
    });

    it('should accept empty html and empty sandboxOrigin', () => {
      expect(validateUiResourceEvent({ ...valid, html: '', sandboxOrigin: '' })).toBe(true);
    });

    it('should return false for null/undefined', () => {
      expect(validateUiResourceEvent(null)).toBe(false);
      expect(validateUiResourceEvent(undefined)).toBe(false);
    });

    it('should return false when type is wrong', () => {
      expect(validateUiResourceEvent({ ...valid, type: 'artifact' })).toBe(false);
    });

    it('should return false for empty toolUseId or resourceUri', () => {
      expect(validateUiResourceEvent({ ...valid, toolUseId: '' })).toBe(false);
      expect(validateUiResourceEvent({ ...valid, resourceUri: '' })).toBe(false);
    });

    it('should return false when csp/permissions are not objects', () => {
      expect(validateUiResourceEvent({ ...valid, csp: null })).toBe(false);
      expect(validateUiResourceEvent({ ...valid, permissions: 'x' })).toBe(false);
    });

    it('should return false for missing fields', () => {
      expect(
        validateUiResourceEvent({ type: 'ui_resource', toolUseId: 'tu-1' }),
      ).toBe(false);
    });
  });

  describe('validateToolInputPartialEvent', () => {
    const valid = {
      type: 'ui_tool_input_partial',
      toolUseId: 'tu-1',
      arguments: { elements: [{ type: 'rect' }] },
    };

    it('should return true for a valid event', () => {
      expect(validateToolInputPartialEvent(valid)).toBe(true);
    });

    it('should accept empty arguments object', () => {
      expect(validateToolInputPartialEvent({ ...valid, arguments: {} })).toBe(true);
    });

    it('should return false for null/undefined or wrong type', () => {
      expect(validateToolInputPartialEvent(null)).toBe(false);
      expect(validateToolInputPartialEvent({ ...valid, type: 'ui_resource' })).toBe(false);
    });

    it('should return false for empty toolUseId', () => {
      expect(validateToolInputPartialEvent({ ...valid, toolUseId: '' })).toBe(false);
    });

    it('should return false when arguments is not a plain object', () => {
      expect(validateToolInputPartialEvent({ ...valid, arguments: [] })).toBe(false);
      expect(validateToolInputPartialEvent({ ...valid, arguments: null })).toBe(false);
      expect(validateToolInputPartialEvent({ ...valid, arguments: 'x' })).toBe(false);
    });
  });

  describe('processStreamEvent', () => {
    let callbacks: StreamParserCallbacks;

    beforeEach(() => {
      callbacks = {
        onMessageStart: vi.fn(),
        onContentBlockStart: vi.fn(),
        onContentBlockDelta: vi.fn(),
        onContentBlockStop: vi.fn(),
        onMessageStop: vi.fn(),
        onToolUse: vi.fn(),
        onToolResult: vi.fn(),
        onQuotaWarning: vi.fn(),
        onQuotaExceeded: vi.fn(),
        onStreamError: vi.fn(),
        onCitation: vi.fn(),
        onArtifact: vi.fn(),
        onUiResource: vi.fn(),
        onToolInputPartial: vi.fn(),
        onParseError: vi.fn(),
        onDone: vi.fn(),
        onError: vi.fn(),
        onMetadata: vi.fn(),
        onReasoning: vi.fn(),
        onToolProgress: vi.fn()
      };
    });

    it('should call onMessageStart for valid message_start', () => {
      const data = { role: 'user' };
      processStreamEvent('message_start', data, callbacks);
      expect(callbacks.onMessageStart).toHaveBeenCalledWith(data);
    });

    it('should call onParseError for invalid message_start', () => {
      processStreamEvent('message_start', { role: 'invalid' }, callbacks);
      expect(callbacks.onParseError).toHaveBeenCalledWith('message_start: invalid data structure');
    });

    it('should call onContentBlockDelta for valid content_block_delta', () => {
      const data = { contentBlockIndex: 0, text: 'hello' };
      processStreamEvent('content_block_delta', data, callbacks);
      expect(callbacks.onContentBlockDelta).toHaveBeenCalledWith(data);
    });

    it('should call onToolUse and onToolProgress for valid tool_use', () => {
      const data = { tool_use: { name: 'search', tool_use_id: 'id123' } };
      processStreamEvent('tool_use', data, callbacks);
      expect(callbacks.onToolUse).toHaveBeenCalledWith(data);
      expect(callbacks.onToolProgress).toHaveBeenCalledWith({
        visible: true,
        toolName: 'search',
        toolUseId: 'id123'
      });
    });

    it('should call onDone and hide tool progress for done event', () => {
      processStreamEvent('done', null, callbacks);
      expect(callbacks.onDone).toHaveBeenCalled();
      expect(callbacks.onToolProgress).toHaveBeenCalledWith({ visible: false });
    });

    it('should call onParseError for invalid event type', () => {
      processStreamEvent('', {}, callbacks);
      expect(callbacks.onParseError).toHaveBeenCalledWith('Invalid event type: must be a non-empty string');
    });

    it('should ignore unknown event types', () => {
      processStreamEvent('unknown_event', {}, callbacks);
      expect(callbacks.onParseError).not.toHaveBeenCalled();
    });

    it('should call onArtifact for a valid artifact event', () => {
      const data = {
        type: 'artifact',
        artifactId: 'art-1',
        version: 2,
        title: 'Report',
        contentType: 'text/html; charset=utf-8',
        sessionId: 'sess-9',
        updatedAt: '2026-05-15T12:00:05+00:00',
        action: 'updated'
      };
      processStreamEvent('artifact', data, callbacks);
      expect(callbacks.onArtifact).toHaveBeenCalledWith(data);
    });

    it('should call onParseError for an invalid artifact event', () => {
      processStreamEvent('artifact', { type: 'artifact', artifactId: '' }, callbacks);
      expect(callbacks.onParseError).toHaveBeenCalledWith('artifact: invalid data structure');
    });

    it('should call onUiResource for a valid ui_resource event', () => {
      const data = {
        type: 'ui_resource',
        toolUseId: 'tu-1',
        resourceUri: 'ui://srv/widget',
        html: '<main>app</main>',
        mimeType: 'text/html;profile=mcp-app',
        csp: {},
        permissions: {},
        sandboxOrigin: ''
      };
      processStreamEvent('ui_resource', data, callbacks);
      expect(callbacks.onUiResource).toHaveBeenCalledWith(data);
    });

    it('should call onParseError for an invalid ui_resource event', () => {
      processStreamEvent('ui_resource', { type: 'ui_resource', toolUseId: '' }, callbacks);
      expect(callbacks.onParseError).toHaveBeenCalledWith('ui_resource: invalid data structure');
    });

    it('should call onToolInputPartial for a valid ui_tool_input_partial event', () => {
      const data = {
        type: 'ui_tool_input_partial',
        toolUseId: 'tu-1',
        arguments: { elements: [{ type: 'rect' }] },
      };
      processStreamEvent('ui_tool_input_partial', data, callbacks);
      expect(callbacks.onToolInputPartial).toHaveBeenCalledWith(data);
    });

    it('should call onParseError for an invalid ui_tool_input_partial event', () => {
      processStreamEvent(
        'ui_tool_input_partial',
        { type: 'ui_tool_input_partial', toolUseId: 'tu-1', arguments: [] },
        callbacks,
      );
      expect(callbacks.onParseError).toHaveBeenCalledWith(
        'ui_tool_input_partial: invalid data structure',
      );
    });
  });

  describe('createStreamLineParser', () => {
    let callbacks: StreamParserCallbacks;
    let parser: ReturnType<typeof createStreamLineParser>;

    beforeEach(() => {
      callbacks = {
        onMessageStart: vi.fn(),
        onParseError: vi.fn()
      };
      parser = createStreamLineParser(callbacks);
    });

    it('should parse event and data lines', () => {
      parser.parseLine('event: message_start');
      parser.parseLine('data: {"role": "user"}');
      expect(callbacks.onMessageStart).toHaveBeenCalledWith({ role: 'user' });
    });

    it('should skip comments and handle empty lines', () => {
      parser.parseLine(': comment');
      // Empty string triggers onParseError because !'' is true
      expect(callbacks.onMessageStart).not.toHaveBeenCalled();
    });

    it('should call onParseError for data without event', () => {
      parser.parseLine('data: {"role": "user"}');
      expect(callbacks.onParseError).toHaveBeenCalledWith('parseLine: received data without preceding event type');
    });

    it('should call onParseError for invalid JSON', () => {
      parser.parseLine('event: message_start');
      parser.parseLine('data: invalid json');
      expect(callbacks.onParseError).toHaveBeenCalledWith(expect.stringContaining('Failed to parse SSE data'));
    });

    it('should reset state', () => {
      parser.parseLine('event: message_start');
      parser.reset();
      parser.parseLine('data: {"role": "user"}');
      expect(callbacks.onParseError).toHaveBeenCalledWith('parseLine: received data without preceding event type');
    });

    it('should call onParseError for empty event type', () => {
      parser.parseLine('event: ');
      expect(callbacks.onParseError).toHaveBeenCalledWith('parseLine: event type cannot be empty');
    });

    it('should skip empty data', () => {
      parser.parseLine('event: message_start');
      parser.parseLine('data: {}');
      parser.parseLine('data: ');
      expect(callbacks.onMessageStart).not.toHaveBeenCalled();
    });
  });

  describe('inferContentBlockType', () => {
    it('should return tool_use for type tool_use', () => {
      expect(inferContentBlockType({ contentBlockIndex: 0, type: 'tool_use' })).toBe('tool_use');
    });

    it('should return tool_use for input field', () => {
      expect(inferContentBlockType({ contentBlockIndex: 0, input: '{}' })).toBe('tool_use');
    });

    it('should return text by default', () => {
      expect(inferContentBlockType({ contentBlockIndex: 0, text: 'hello' })).toBe('text');
      expect(inferContentBlockType({ contentBlockIndex: 0 })).toBe('text');
    });
  });

  describe('extractStreamingStringField', () => {
    it('returns null when input is empty', () => {
      expect(extractStreamingStringField('', 'content')).toBeNull();
    });

    it('returns null when the field has not started streaming', () => {
      expect(extractStreamingStringField('{"title":"Hi"', 'content')).toBeNull();
      expect(extractStreamingStringField('{"title":"Hi","content"', 'content')).toBeNull();
      expect(extractStreamingStringField('{"title":"Hi","content":', 'content')).toBeNull();
    });

    it('returns the partial value while the string is still open', () => {
      expect(
        extractStreamingStringField('{"title":"Hi","content":"<!DOCTYPE htm', 'content'),
      ).toBe('<!DOCTYPE htm');
    });

    it('returns the full value once the closing quote arrives', () => {
      expect(
        extractStreamingStringField('{"content":"<h1>Hello</h1>","x":1}', 'content'),
      ).toBe('<h1>Hello</h1>');
    });

    it('decodes JSON string escapes', () => {
      expect(
        extractStreamingStringField('{"content":"line1\\nline2\\t\\"q\\"\\\\","', 'content'),
      ).toBe('line1\nline2\t"q"\\');
    });

    it('decodes unicode escapes', () => {
      expect(extractStreamingStringField('{"content":"\\u00e9\\u4e2d', 'content')).toBe(
        'é中',
      );
    });

    it('drops a dangling backslash that has not finished streaming', () => {
      expect(extractStreamingStringField('{"content":"abc\\', 'content')).toBe('abc');
    });

    it('drops an incomplete unicode escape', () => {
      expect(extractStreamingStringField('{"content":"abc\\u00e', 'content')).toBe('abc');
    });

    it('does not match a different field with a shared prefix', () => {
      // `content_type` must not be mistaken for `content`
      expect(
        extractStreamingStringField('{"content_type":"text/html","content":"body', 'content'),
      ).toBe('body');
    });

    it('tolerates whitespace between key, colon, and value', () => {
      expect(extractStreamingStringField('{"content"  :  "hi', 'content')).toBe('hi');
    });

    it('returns empty string for an empty completed value', () => {
      expect(extractStreamingStringField('{"content":""}', 'content')).toBe('');
    });
  });

  describe('parseToolResultContent', () => {
    it('should parse text content', () => {
      const result = parseToolResultContent([{ text: 'hello world' }]);
      expect(result).toEqual([{ text: 'hello world' }]);
    });

    it('should parse JSON content from text', () => {
      const result = parseToolResultContent([{ text: '{"key": "value"}' }]);
      expect(result).toEqual([{ json: { key: 'value' } }]);
    });

    it('should parse image content with source.data', () => {
      const result = parseToolResultContent([{
        image: {
          format: 'png',
          source: { data: 'base64data' }
        }
      }]);
      expect(result).toEqual([{
        image: { format: 'png', data: 'base64data' }
      }]);
    });

    it('should parse image content with direct data', () => {
      const result = parseToolResultContent([{
        image: {
          format: 'jpeg',
          data: 'base64data'
        }
      }]);
      expect(result).toEqual([{
        image: { format: 'jpeg', data: 'base64data' }
      }]);
    });

    it('should parse direct JSON content', () => {
      const result = parseToolResultContent([{ json: { key: 'value' } }]);
      expect(result).toEqual([{ json: { key: 'value' } }]);
    });

    it('should skip invalid items', () => {
      const result = parseToolResultContent([null, 'string', {}]);
      expect(result).toEqual([]);
    });

    it('should default image format to png', () => {
      const result = parseToolResultContent([{
        image: { source: { data: 'base64data' } }
      }]);
      expect(result).toEqual([{
        image: { format: 'png', data: 'base64data' }
      }]);
    });
  });
});