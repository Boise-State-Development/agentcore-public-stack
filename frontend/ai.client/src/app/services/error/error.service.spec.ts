import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ErrorService, ErrorCode, StreamErrorEvent, ConversationalStreamError } from './error.service';

describe('ErrorService', () => {
  let service: ErrorService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [ErrorService]
    });
    service = TestBed.inject(ErrorService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('handleHttpError', () => {
    it('should handle 400 Bad Request', () => {
      const error = { status: 400, error: { detail: 'Invalid input' }, message: 'Bad Request' };
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Invalid Request');
      expect(result.message).toBe('Invalid input');
      expect(result.code).toBe(ErrorCode.BAD_REQUEST);
      expect(service.errorMessages()).toHaveLength(1);
    });

    it('should handle 401 Unauthorized', () => {
      const error = { status: 401, error: { detail: 'Token expired' }, message: 'Unauthorized' };
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Authentication Required');
      expect(result.code).toBe(ErrorCode.UNAUTHORIZED);
    });

    it('should handle 403 Forbidden', () => {
      const error = { status: 403, error: { detail: 'Access denied' }, message: 'Forbidden' };
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Access Denied');
      expect(result.code).toBe(ErrorCode.FORBIDDEN);
    });

    it('never includes the request URL in the technical details', () => {
      const url = 'https://host/api/tools/secret_tool/capabilities';
      const withDetail = service.handleHttpError({
        status: 403, error: { detail: 'Access denied' }, message: `Http failure response for ${url}`, url,
      } as never);
      const withoutDetail = service.handleHttpError({
        status: 500, error: null, message: 'boom', url,
      } as never);

      for (const result of [withDetail, withoutDetail]) {
        expect(result.detail).toContain('Status:');
        expect(result.detail).not.toContain('URL:');
        expect(result.detail).not.toContain('secret_tool');
        expect(result.detail).not.toContain('https://host');
      }
    });

    it('should handle 404 Not Found', () => {
      const error = { status: 404, error: { detail: 'Resource not found' }, message: 'Not Found' };
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Not Found');
      expect(result.code).toBe(ErrorCode.NOT_FOUND);
    });

    it('should handle 500 Internal Server Error', () => {
      const error = { status: 500, error: { detail: 'Database connection failed' }, message: 'Internal Server Error' };
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Server Error');
      expect(result.code).toBe(ErrorCode.INTERNAL_ERROR);
    });

    it('should handle 503 Service Unavailable', () => {
      const error = { status: 503, error: { detail: 'Service temporarily down' }, message: 'Service Unavailable' };
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Service Unavailable');
      expect(result.code).toBe(ErrorCode.SERVICE_UNAVAILABLE);
    });

    it('should handle nested error structure', () => {
      const error = {
        status: 400,
        error: { error: { detail: 'Validation failed', code: 'validation_error' } },
        message: 'Bad Request'
      };
      
      const result = service.handleHttpError(error);
      
      expect(result.message).toBe('Validation failed');
      expect(result.detail).toContain('Status: 400');
    });

    it('should handle error with message field', () => {
      const error = { status: 400, error: { message: 'Custom error message' }, message: 'Bad Request' };
      
      const result = service.handleHttpError(error);
      
      expect(result.message).toBe('Custom error message');
    });

    it('should fallback to status-based message when no detail', () => {
      const error = { status: 400, error: {}, message: 'Bad Request' };
      
      const result = service.handleHttpError(error);
      
      expect(result.message).toBe('The request was invalid. Please check your input and try again.');
    });

    it('should handle Error objects', () => {
      const error = new Error('Network timeout');
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Error');
      expect(result.message).toBe('An error occurred');
      expect(result.detail).toBe('Network timeout');
      expect(result.code).toBe(ErrorCode.UNKNOWN_ERROR);
    });

    it('should handle unknown error types', () => {
      const error = 'String error';
      
      const result = service.handleHttpError(error);
      
      expect(result.title).toBe('Error');
      expect(result.message).toBe('An unknown error occurred');
      expect(result.detail).toBe('String error');
      expect(result.code).toBe(ErrorCode.UNKNOWN_ERROR);
    });
  });

  describe('handleStreamError', () => {
    it('should handle stream error event', () => {
      const errorEvent: StreamErrorEvent = {
        error: 'Stream connection lost',
        code: ErrorCode.STREAM_ERROR,
        detail: 'WebSocket closed unexpectedly',
        recoverable: true,
        metadata: { attempt: 1 }
      };
      
      const result = service.handleStreamError(errorEvent);
      
      expect(result.title).toBe('Stream Error');
      expect(result.message).toBe('Stream connection lost');
      expect(result.detail).toBe('WebSocket closed unexpectedly');
      expect(result.code).toBe(ErrorCode.STREAM_ERROR);
      expect(result.actionLabel).toBe('Retry');
    });

    it('should not add retry action for non-recoverable errors', () => {
      const errorEvent: StreamErrorEvent = {
        error: 'Fatal error',
        code: ErrorCode.AGENT_ERROR,
        recoverable: false
      };
      
      const result = service.handleStreamError(errorEvent);
      
      expect(result.actionLabel).toBeUndefined();
    });
  });

  describe('handleConversationalStreamError', () => {
    it('should handle conversational stream error', () => {
      const errorEvent: ConversationalStreamError = {
        type: 'stream_error',
        code: ErrorCode.MODEL_ERROR,
        message: 'Model is temporarily unavailable',
        recoverable: true,
        retry_after: 30
      };
      
      service.handleConversationalStreamError(errorEvent);
      
      const lastError = service.lastError();
      expect(lastError?.title).toBe('Model Error');
      expect(lastError?.detail).toBe('Model is temporarily unavailable');
      expect(service.errorMessages()).toHaveLength(0); // Not added to display list
    });
  });

  describe('handleNetworkError', () => {
    it('should handle network error with custom message', () => {
      const result = service.handleNetworkError('Connection timeout');
      
      expect(result.title).toBe('Network Error');
      expect(result.message).toBe('Network connection error');
      expect(result.detail).toBe('Connection timeout');
      expect(result.code).toBe(ErrorCode.NETWORK_ERROR);
    });

    it('should handle network error with default message', () => {
      const result = service.handleNetworkError();
      
      expect(result.message).toBe('Network connection error');
      expect(result.detail).toBe('Unable to connect to the server. Please check your connection.');
    });
  });

  describe('addError', () => {
    it('should add custom error', () => {
      const result = service.addError('Custom Title', 'Custom message', 'Additional details', ErrorCode.VALIDATION_ERROR);
      
      expect(result.title).toBe('Validation Error');
      expect(result.message).toBe('Custom Title');
      expect(result.detail).toBe('Additional details');
      expect(result.code).toBe(ErrorCode.VALIDATION_ERROR);
    });
  });

  describe('dismissError', () => {
    it('should dismiss error by ID', () => {
      const error1 = service.addError('Error 1', 'Message 1');
      const error2 = service.addError('Error 2', 'Message 2');
      
      service.dismissError(error1.id);
      
      const remaining = service.errorMessages();
      expect(remaining).toHaveLength(1);
      expect(remaining[0].id).toBe(error2.id);
    });

    it('should clear last error when dismissed', () => {
      const error = service.addError('Test', 'Message');
      
      service.dismissError(error.id);
      
      expect(service.lastError()).toBeNull();
    });

    it('should handle dismiss of non-existent ID', () => {
      service.addError('Test', 'Message');
      
      service.dismissError('non-existent');
      
      expect(service.errorMessages()).toHaveLength(1);
    });
  });

  describe('clearAllErrors', () => {
    it('should clear all errors and last error', () => {
      service.addError('Error 1', 'Message 1');
      service.addError('Error 2', 'Message 2');
      
      service.clearAllErrors();
      
      expect(service.errorMessages()).toHaveLength(0);
      expect(service.lastError()).toBeNull();
    });
  });

  describe('private methods (tested indirectly)', () => {
    it('should generate unique error IDs', () => {
      const error1 = service.addError('Test 1', 'Message 1');
      const error2 = service.addError('Test 2', 'Message 2');
      
      expect(error1.id).not.toBe(error2.id);
      expect(error1.id).toMatch(/^error-\d+-[a-z0-9]+$/);
    });

    it('should test getStatusInfo indirectly through HTTP errors', () => {
      const error422 = { status: 422, error: {}, message: 'Unprocessable Entity' };
      const result = service.handleHttpError(error422);
      
      expect(result.title).toBe('Validation Error');
      expect(result.code).toBe(ErrorCode.VALIDATION_ERROR);
    });

    it('should test getErrorTitle indirectly through addError', () => {
      const result = service.addError('Test', 'Message', undefined, ErrorCode.TOOL_ERROR);
      
      expect(result.title).toBe('Tool Error');
    });
  });
});