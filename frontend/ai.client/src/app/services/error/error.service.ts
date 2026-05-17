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
  MAX_TOKENS = 'max_tokens',

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
 * Stream error event matching backend StreamErrorEvent (legacy)
 */
export interface StreamErrorEvent {
  error: string;
  code: ErrorCode;
  detail?: string;
  recoverable: boolean;
  metadata?: Record<string, unknown>;
}

/**
 * Conversational stream error event matching backend ConversationalErrorEvent.
 * This is sent when errors are streamed as assistant messages for better UX.
 */
export interface ConversationalStreamError {
  type: 'stream_error';
  code: ErrorCode;
  message: string;  // Markdown-formatted message (already displayed as assistant message)
  recoverable: boolean;
  retry_after?: number;  // Snake case to match backend Pydantic model
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
      const errorBody = error.error;

      // Check if error body has a detail field (e.g., { detail: "..." })
      let userMessage: string | undefined;
      let technicalDetails: string | undefined;

      if (errorBody && typeof errorBody === 'object') {
        const errorObj = errorBody as Record<string, unknown>;
        
        // Priority 1: Check for error.detail (direct detail field) - most common case
        if ('detail' in errorObj && typeof errorObj['detail'] === 'string') {
          userMessage = errorObj['detail'] as string;
          technicalDetails = this.buildTechnicalDetails(error);
        }
        // Priority 2: Check for error.error.detail (structured error from backend)
        else if ('error' in errorObj && errorObj['error'] && typeof errorObj['error'] === 'object') {
          const nestedError = errorObj['error'] as Record<string, unknown>;
          if ('detail' in nestedError && typeof nestedError['detail'] === 'string') {
            userMessage = nestedError['detail'] as string;
            // Create ErrorDetail object if we have the necessary fields
            const structuredError: ErrorDetail | undefined = 
              ('code' in nestedError || 'message' in nestedError)
                ? (nestedError as unknown as ErrorDetail)
                : undefined;
            technicalDetails = this.buildTechnicalDetails(error, structuredError);
          } else if ('message' in nestedError && typeof nestedError['message'] === 'string') {
            userMessage = nestedError['message'] as string;
            technicalDetails = this.buildTechnicalDetails(error);
          }
        }
        // Priority 3: Check for error.message (alternative field)
        else if ('message' in errorObj && typeof errorObj['message'] === 'string') {
          userMessage = errorObj['message'] as string;
          technicalDetails = this.buildTechnicalDetails(error);
        }
      }

      // If we found a user message, use it; otherwise fallback to status-based message
      if (userMessage) {
        const statusInfo = this.getStatusInfo(status);
        errorMessage = {
          id: this.generateErrorId(),
          title: statusInfo.title,
          message: userMessage,
          detail: technicalDetails,
          code: statusInfo.code,
          timestamp: new Date(),
          dismissible: true,
        };
      } else {
        // Fallback to status-based message
        errorMessage = this.createErrorMessageFromStatus(status, error.message);
        // Add technical details to the detail field
        if (!errorMessage.detail) {
          errorMessage.detail = this.buildTechnicalDetails(error);
        }
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
   * Handle SSE stream error event (legacy)
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
   * Handle conversational stream error event.
   *
   * Note: The error message is already displayed as an assistant message in the chat,
   * so we don't need to show it again. This method is for tracking error state
   * and potentially enabling retry functionality.
   */
  handleConversationalStreamError(errorEvent: ConversationalStreamError): void {
    // Store the error state for potential retry functionality
    const errorMessage: ErrorMessage = {
      id: this.generateErrorId(),
      title: this.getErrorTitle(errorEvent.code),
      message: 'An error occurred during processing', // Brief summary, full message is in chat
      detail: errorEvent.message,
      code: errorEvent.code,
      timestamp: new Date(),
      dismissible: true,
    };

    // Only store it, don't display as toast (message is already in chat)
    this.lastErrorSignal.set(errorMessage);

    // If there's a retry delay, we could use it for auto-retry logic
    if (errorEvent.retry_after) {
      // Future: implement auto-retry with delay
      console.log(`Error is recoverable, retry after ${errorEvent.retry_after}s`);
    }
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
      [ErrorCode.MAX_TOKENS]: 'Response Truncated',
      [ErrorCode.NETWORK_ERROR]: 'Network Error',
      [ErrorCode.UNKNOWN_ERROR]: 'Error',
    };

    return titles[code] || 'Error';
  }

  /**
   * Create error message from HTTP status code
   * Used as fallback when no detail is found in the error response
   */
  private createErrorMessageFromStatus(status: number, defaultMessage: string): ErrorMessage {
    const statusInfo = this.getStatusInfo(status);
    const fallbackMessages: Record<number, string> = {
      400: 'The request was invalid. Please check your input and try again.',
      401: 'Please log in to continue.',
      403: 'You do not have permission to perform this action.',
      404: 'The requested resource was not found.',
      409: 'The request conflicts with the current state.',
      422: 'The submitted data is invalid.',
      429: 'Too many requests. Please slow down and try again later.',
      500: 'An internal server error occurred. Please try again later.',
      503: 'The service is temporarily unavailable. Please try again later.',
      504: 'The request took too long to complete. Please try again.',
    };

    const message = fallbackMessages[status] || 'An error occurred. Please try again.';
    return this.createErrorMessage(message, defaultMessage, statusInfo.code);
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
   * Build technical details string from HTTP error response
   * This includes technical information that should be hidden in the details section
   */
  private buildTechnicalDetails(
    error: { status: number; error: unknown; message: string; url?: string },
    structuredError?: ErrorDetail
  ): string {
    const details: string[] = [];

    // Add status information
    details.push(`Status: ${error.status} ${this.getStatusText(error.status)}`);

    // Add URL if available
    if (error.url) {
      details.push(`URL: ${error.url}`);
    }

    // Add HTTP error message (technical, not user-facing)
    if (error.message) {
      details.push(`HTTP Message: ${error.message}`);
    }

    // Add structured error details if available
    if (structuredError) {
      if (structuredError.code) {
        details.push(`Error Code: ${structuredError.code}`);
      }
      if (structuredError.field) {
        details.push(`Field: ${structuredError.field}`);
      }
      if (structuredError.metadata) {
        details.push(`Metadata: ${JSON.stringify(structuredError.metadata, null, 2)}`);
      }
    }

    // Add raw error body for debugging (excluding the detail we're already showing)
    if (error.error && typeof error.error === 'object') {
      const errorObj = error.error as Record<string, unknown>;
      // Create a copy without the detail field to avoid duplication
      const errorCopy = { ...errorObj };
      delete errorCopy['detail'];
      if (Object.keys(errorCopy).length > 0) {
        details.push(`\nFull Response:\n${JSON.stringify(error.error, null, 2)}`);
      }
    }

    return details.join('\n');
  }

  /**
   * Get status text for HTTP status code
   */
  private getStatusText(status: number): string {
    const statusTexts: Record<number, string> = {
      400: 'Bad Request',
      401: 'Unauthorized',
      403: 'Forbidden',
      404: 'Not Found',
      409: 'Conflict',
      422: 'Unprocessable Entity',
      429: 'Too Many Requests',
      500: 'Internal Server Error',
      503: 'Service Unavailable',
      504: 'Gateway Timeout',
    };
    return statusTexts[status] || 'Unknown';
  }

  /**
   * Get status info (title and code) for HTTP status code
   */
  private getStatusInfo(status: number): { title: string; code: ErrorCode } {
    const statusMessages: Record<number, { title: string; code: ErrorCode }> = {
      400: { title: 'Invalid Request', code: ErrorCode.BAD_REQUEST },
      401: { title: 'Authentication Required', code: ErrorCode.UNAUTHORIZED },
      403: { title: 'Access Denied', code: ErrorCode.FORBIDDEN },
      404: { title: 'Not Found', code: ErrorCode.NOT_FOUND },
      409: { title: 'Conflict', code: ErrorCode.CONFLICT },
      422: { title: 'Validation Error', code: ErrorCode.VALIDATION_ERROR },
      429: { title: 'Rate Limit Exceeded', code: ErrorCode.RATE_LIMIT_EXCEEDED },
      500: { title: 'Server Error', code: ErrorCode.INTERNAL_ERROR },
      503: { title: 'Service Unavailable', code: ErrorCode.SERVICE_UNAVAILABLE },
      504: { title: 'Request Timeout', code: ErrorCode.TIMEOUT },
    };

    return statusMessages[status] || { title: 'Error', code: ErrorCode.UNKNOWN_ERROR };
  }

  /**
   * Type guard for HTTP error response
   */
  private isHttpErrorResponse(error: unknown): error is { status: number; error: unknown; message: string; url?: string } {
    return (
      typeof error === 'object' &&
      error !== null &&
      'status' in error &&
      typeof (error as { status: unknown }).status === 'number'
    );
  }
}
