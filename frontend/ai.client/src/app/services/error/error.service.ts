import { Injectable, signal } from '@angular/core';

/**
 * Error codes matching backend ErrorCode enum
 */
export enum ErrorCode {
  // Client errors (4xx)
  BAD_REQUEST = 'bad_request',
  UNAUTHORIZED = 'unauthorized',
  FORBIDDEN = 'forbidden',
  NOT_FOUND = 'not_found',
  CONFLICT = 'conflict',
  VALIDATION_ERROR = 'validation_error',
  RATE_LIMIT_EXCEEDED = 'rate_limit_exceeded',

  // Server errors (5xx)
  INTERNAL_ERROR = 'internal_error',
  SERVICE_UNAVAILABLE = 'service_unavailable',
  TIMEOUT = 'timeout',

  // Agent-specific errors
  AGENT_ERROR = 'agent_error',
  TOOL_ERROR = 'tool_error',
  MODEL_ERROR = 'model_error',
  STREAM_ERROR = 'stream_error',

  // Client-side errors
  NETWORK_ERROR = 'network_error',
  UNKNOWN_ERROR = 'unknown_error',
}

/**
 * Structured error detail matching backend ErrorDetail
 */
export interface ErrorDetail {
  code: ErrorCode;
  message: string;
  detail?: string;
  field?: string;
  metadata?: Record<string, unknown>;
}

/**
 * Stream error event matching backend StreamErrorEvent
 */
export interface StreamErrorEvent {
  error: string;
  code: ErrorCode;
  detail?: string;
  recoverable: boolean;
  metadata?: Record<string, unknown>;
}

/**
 * UI-displayable error message
 */
export interface ErrorMessage {
  id: string;
  title: string;
  message: string;
  detail?: string;
  code?: ErrorCode;
  timestamp: Date;
  dismissible: boolean;
  actionLabel?: string;
  actionCallback?: () => void;
}

/**
 * Centralized error handling service
 *
 * Responsibilities:
 * - Parse errors from different sources (HTTP, SSE, network)
 * - Convert errors to user-friendly messages
 * - Maintain error state for UI display
 * - Provide error notification capabilities
 */
@Injectable({
  providedIn: 'root'
})
export class ErrorService {
  // Active error messages (for displaying in UI)
  private errorMessagesSignal = signal<ErrorMessage[]>([]);
  public errorMessages = this.errorMessagesSignal.asReadonly();

  // Last error (for quick access)
  private lastErrorSignal = signal<ErrorMessage | null>(null);
  public lastError = this.lastErrorSignal.asReadonly();

  /**
   * Parse HTTP error response and create user-friendly message
   */
  handleHttpError(error: unknown): ErrorMessage {
    let errorMessage: ErrorMessage;

    if (this.isHttpErrorResponse(error)) {
      const status = error.status;
      const detail = error.error;

      // Check if backend sent structured error
      if (detail && typeof detail === 'object' && 'error' in detail) {
        const structuredError = detail.error as ErrorDetail;
        errorMessage = this.createErrorMessage(
          structuredError.message,
          structuredError.detail,
          structuredError.code
        );
      } else {
        // Fallback to status-based message
        errorMessage = this.createErrorMessageFromStatus(status, error.message);
      }
    } else if (error instanceof Error) {
      errorMessage = this.createErrorMessage(
        'An error occurred',
        error.message,
        ErrorCode.UNKNOWN_ERROR
      );
    } else {
      errorMessage = this.createErrorMessage(
        'An unknown error occurred',
        String(error),
        ErrorCode.UNKNOWN_ERROR
      );
    }

    this.addErrorMessage(errorMessage);
    return errorMessage;
  }

  /**
   * Handle SSE stream error event
   */
  handleStreamError(errorEvent: StreamErrorEvent): ErrorMessage {
    const errorMessage = this.createErrorMessage(
      errorEvent.error,
      errorEvent.detail,
      errorEvent.code,
      errorEvent.metadata
    );

    // Add retry action if error is recoverable
    if (errorEvent.recoverable) {
      errorMessage.actionLabel = 'Retry';
      // Callback should be set by caller
    }

    this.addErrorMessage(errorMessage);
    return errorMessage;
  }

  /**
   * Handle network/connection errors
   */
  handleNetworkError(message?: string): ErrorMessage {
    const errorMessage = this.createErrorMessage(
      'Network connection error',
      message || 'Unable to connect to the server. Please check your connection.',
      ErrorCode.NETWORK_ERROR
    );

    this.addErrorMessage(errorMessage);
    return errorMessage;
  }

  /**
   * Add a custom error message
   */
  addError(title: string, message: string, detail?: string, code?: ErrorCode): ErrorMessage {
    const errorMessage = this.createErrorMessage(title, message, code, undefined, detail);
    this.addErrorMessage(errorMessage);
    return errorMessage;
  }

  /**
   * Dismiss an error message by ID
   */
  dismissError(id: string): void {
    this.errorMessagesSignal.update(messages =>
      messages.filter(msg => msg.id !== id)
    );

    // Clear last error if it was dismissed
    const lastError = this.lastErrorSignal();
    if (lastError?.id === id) {
      this.lastErrorSignal.set(null);
    }
  }

  /**
   * Clear all error messages
   */
  clearAllErrors(): void {
    this.errorMessagesSignal.set([]);
    this.lastErrorSignal.set(null);
  }

  /**
   * Generate user-friendly error title from error code
   */
  private getErrorTitle(code: ErrorCode): string {
    const titles: Record<ErrorCode, string> = {
      [ErrorCode.BAD_REQUEST]: 'Invalid Request',
      [ErrorCode.UNAUTHORIZED]: 'Authentication Required',
      [ErrorCode.FORBIDDEN]: 'Access Denied',
      [ErrorCode.NOT_FOUND]: 'Not Found',
      [ErrorCode.CONFLICT]: 'Conflict',
      [ErrorCode.VALIDATION_ERROR]: 'Validation Error',
      [ErrorCode.RATE_LIMIT_EXCEEDED]: 'Rate Limit Exceeded',
      [ErrorCode.INTERNAL_ERROR]: 'Server Error',
      [ErrorCode.SERVICE_UNAVAILABLE]: 'Service Unavailable',
      [ErrorCode.TIMEOUT]: 'Request Timeout',
      [ErrorCode.AGENT_ERROR]: 'Agent Error',
      [ErrorCode.TOOL_ERROR]: 'Tool Error',
      [ErrorCode.MODEL_ERROR]: 'Model Error',
      [ErrorCode.STREAM_ERROR]: 'Stream Error',
      [ErrorCode.NETWORK_ERROR]: 'Network Error',
      [ErrorCode.UNKNOWN_ERROR]: 'Error',
    };

    return titles[code] || 'Error';
  }

  /**
   * Create error message from HTTP status code
   */
  private createErrorMessageFromStatus(status: number, defaultMessage: string): ErrorMessage {
    const statusMessages: Record<number, { title: string; message: string; code: ErrorCode }> = {
      400: {
        title: 'Invalid Request',
        message: 'The request was invalid. Please check your input and try again.',
        code: ErrorCode.BAD_REQUEST
      },
      401: {
        title: 'Authentication Required',
        message: 'Please log in to continue.',
        code: ErrorCode.UNAUTHORIZED
      },
      403: {
        title: 'Access Denied',
        message: 'You do not have permission to perform this action.',
        code: ErrorCode.FORBIDDEN
      },
      404: {
        title: 'Not Found',
        message: 'The requested resource was not found.',
        code: ErrorCode.NOT_FOUND
      },
      409: {
        title: 'Conflict',
        message: 'The request conflicts with the current state.',
        code: ErrorCode.CONFLICT
      },
      422: {
        title: 'Validation Error',
        message: 'The submitted data is invalid.',
        code: ErrorCode.VALIDATION_ERROR
      },
      429: {
        title: 'Rate Limit Exceeded',
        message: 'Too many requests. Please slow down and try again later.',
        code: ErrorCode.RATE_LIMIT_EXCEEDED
      },
      500: {
        title: 'Server Error',
        message: 'An internal server error occurred. Please try again later.',
        code: ErrorCode.INTERNAL_ERROR
      },
      503: {
        title: 'Service Unavailable',
        message: 'The service is temporarily unavailable. Please try again later.',
        code: ErrorCode.SERVICE_UNAVAILABLE
      },
      504: {
        title: 'Request Timeout',
        message: 'The request took too long to complete. Please try again.',
        code: ErrorCode.TIMEOUT
      },
    };

    const statusInfo = statusMessages[status];
    if (statusInfo) {
      return this.createErrorMessage(statusInfo.message, defaultMessage, statusInfo.code);
    }

    return this.createErrorMessage(
      'An error occurred',
      defaultMessage || `Request failed with status ${status}`,
      ErrorCode.UNKNOWN_ERROR
    );
  }

  /**
   * Create an ErrorMessage object
   */
  private createErrorMessage(
    message: string,
    detail?: string,
    code?: ErrorCode,
    metadata?: Record<string, unknown>,
    customDetail?: string
  ): ErrorMessage {
    const errorCode = code || ErrorCode.UNKNOWN_ERROR;
    return {
      id: this.generateErrorId(),
      title: this.getErrorTitle(errorCode),
      message,
      detail: customDetail || detail,
      code: errorCode,
      timestamp: new Date(),
      dismissible: true,
    };
  }

  /**
   * Add error message to state
   */
  private addErrorMessage(error: ErrorMessage): void {
    this.errorMessagesSignal.update(messages => [...messages, error]);
    this.lastErrorSignal.set(error);
  }

  /**
   * Generate unique error ID
   */
  private generateErrorId(): string {
    return `error-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }

  /**
   * Type guard for HTTP error response
   */
  private isHttpErrorResponse(error: unknown): error is { status: number; error: unknown; message: string } {
    return (
      typeof error === 'object' &&
      error !== null &&
      'status' in error &&
      typeof (error as { status: unknown }).status === 'number'
    );
  }
}
