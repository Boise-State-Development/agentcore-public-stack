import { inject, Injectable, OnDestroy } from '@angular/core';
import { Router } from '@angular/router';
import { v4 as uuidv4 } from 'uuid';
import { ChatStateService } from './chat-state.service';
import { ChatHttpService } from './chat-http.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { UserService } from '../../../auth/user.service';
import { ModelService } from '../model/model.service';
import { ToolService } from '../../../services/tool/tool.service';
import { FileUploadService } from '../../../services/file-upload';
import { FileAttachmentData } from '../models/message.model';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ErrorService } from '../../../services/error/error.service';
import { StreamParserService } from './stream-parser.service';
import { HttpErrorResponse } from '@angular/common/http';

export interface ContentFile {
  fileName: string;
  fileSize: number;
  contentType: string;
  s3Key: string;
}

@Injectable({
  providedIn: 'root',
})
export class ChatRequestService implements OnDestroy {
  // private conversationService = inject(ConversationService);
  private chatHttpService = inject(ChatHttpService);
  private chatStateService = inject(ChatStateService);
  private messageMapService = inject(MessageMapService);
  private sessionService = inject(SessionService);
  private userService = inject(UserService);
  private modelService = inject(ModelService);
  private toolService = inject(ToolService);
  private fileUploadService = inject(FileUploadService);
  private oauthConsentService = inject(OAuthConsentService);
  private streamParserService = inject(StreamParserService);
  private errorService = inject(ErrorService);
  private router = inject(Router);
  // TODO: Inject proper logging service

  /** Last request payload — replayed (with `interrupt_responses` added) when
   *  the user completes an OAuth consent so the paused agent turn resumes
   *  without retyping. Cleared on a true new turn. */
  private lastRequestObject: Record<string, unknown> | null = null;

  constructor() {
    this.oauthConsentService.setResumeHandler((interruptIds, context) =>
      this.resumeFromOAuthConsent(interruptIds, context?.sessionId),
    );
  }

  ngOnDestroy(): void {
    this.oauthConsentService.setResumeHandler(null);
  }

  async submitChatRequest(
    userInput: string,
    sessionId: string | null,
    fileUploadIds?: string[],
    assistantId?: string,
  ): Promise<void> {
    // Ensure conversation exists and get its ID
    // Update URL to reflect current conversation
    const isNewSession = !sessionId;
    sessionId = sessionId || uuidv4();

    // If this is a new session, add it to the session cache optimistically
    // IMPORTANT: This must happen BEFORE navigation to prevent a race condition
    // where the route subscription tries to fetch metadata before the session
    // is marked as "new" in the newSessionIds set
    if (isNewSession) {
      // Get the current user from UserService
      const user = this.userService.getUser();
      const userId = user?.user_id || 'anonymous';

      // Add the new session to the cache so it appears in the sidenav immediately
      this.sessionService.addSessionToCache(sessionId, userId);
    }

    // Preserve assistantId in URL when navigating to new session
    this.navigateToSession(sessionId, assistantId);

    // Get file attachment metadata for display in user message
    const fileAttachments = this.getFileAttachments(fileUploadIds);

    // Create and add user message with file attachments
    this.messageMapService.addUserMessage(sessionId, userInput, fileAttachments);

    // Start streaming for this conversation
    this.messageMapService.startStreaming(sessionId);

    // Build and send request with file upload IDs and assistant ID
    const requestObject = this.buildChatRequestObject(
      userInput,
      sessionId,
      fileUploadIds,
      assistantId,
    );

    // Remember this turn's params so the OAuth resume handler can replay
    // them with `interrupt_responses` attached. Snapshotting the *exact*
    // payload keeps the agent cache key stable, so the resume hits the
    // same paused agent instance.
    this.lastRequestObject = { ...requestObject };

    try {
      await this.chatHttpService.sendChatRequest(requestObject);
    } catch (error) {
      // TODO: Replace with proper logging service
      // logger.error('Chat request failed', { error, conversationId: sessionId });
      this.chatStateService.setChatLoading(false);
      this.messageMapService.endStreaming();
      throw error; // Re-throw to allow caller to handle
    }
  }

  /**
   * Navigates to the conversation route
   * @param sessionId The conversation ID to navigate to
   * @param assistantId Optional assistant ID to preserve in query params
   */
  private navigateToSession(sessionId: string, assistantId?: string): void {
    // Build query params - only include assistantId if it has a value
    const queryParams: Record<string, string> = {};
    if (assistantId) {
      queryParams['assistantId'] = assistantId;
    }

    this.router.navigate(['s', sessionId], {
      replaceUrl: true,
      queryParams,
      queryParamsHandling: 'merge',
    });
  }

  private buildChatRequestObject(
    message: string,
    session_id: string,
    fileUploadIds?: string[],
    assistantId?: string,
  ) {
    const selectedModel = this.modelService.getSelectedModel();

    if (!selectedModel) {
      throw new Error('No model selected. Please select a model before sending a message.');
    }

    // If using the system default model, send null for model_id to let backend use its default
    const isDefaultModel = this.modelService.isUsingDefaultModel();

    // Get enabled tools from tool service (RBAC-based)
    const enabledTools = this.toolService.getEnabledToolIds();

    const requestObject: Record<string, unknown> = {
      message,
      session_id,
      model_id: isDefaultModel ? null : selectedModel.modelId,
      enabled_tools: enabledTools,
      provider: isDefaultModel ? null : selectedModel.provider,
    };

    // Add file upload IDs if present
    if (fileUploadIds && fileUploadIds.length > 0) {
      requestObject['file_upload_ids'] = fileUploadIds;
    }

    // Add assistant ID if present
    // NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
    // AgentCore Runtime's internal 'assistant_id' field handling (causes 424 error)
    if (assistantId) {
      requestObject['rag_assistant_id'] = assistantId;
    }

    return requestObject;
  }

  /**
   * Replay the last turn's request with `interrupt_responses` attached so
   * the backend resumes the paused agent turn instead of starting a new
   * one. Triggered by OAuthConsentService after the user completes a
   * consent popup.
   */
  private async resumeFromOAuthConsent(
    interruptIds: string[],
    fallbackSessionId?: string,
  ): Promise<void> {
    if (interruptIds.length === 0) {
      return;
    }

    // Live flow: the same tab that originated the turn still has its
    // payload — replay it with `interrupt_responses` attached.
    // Refresh flow: ``lastRequestObject`` is null, so we synthesize a
    // minimal resume payload from the consent service's session context.
    const liveSessionId = this.lastRequestObject?.['session_id'] as string | undefined;
    const sessionId = liveSessionId ?? fallbackSessionId;
    if (!sessionId) {
      return;
    }

    // Reset the parser so the resumed stream is treated as a fresh batch
    // of events. Without this, the parser stays in Completed state from
    // the prior `done` and ignores everything.
    this.streamParserService.reset(sessionId);
    this.messageMapService.startStreaming(sessionId);
    this.chatStateService.createNewAbortController();
    this.chatStateService.setChatLoading(true);

    const baseRequest: Record<string, unknown> = this.lastRequestObject
      ? { ...this.lastRequestObject }
      : { session_id: sessionId };

    const resumeRequest: Record<string, unknown> = {
      ...baseRequest,
      // The original prompt is already in the agent's interrupt context;
      // sending an empty string keeps the request valid without
      // re-augmenting or re-charging quota.
      message: '',
      interrupt_responses: interruptIds.map((interruptId) => ({
        interruptId,
        // The token is already in AgentCore Identity's vault by the time
        // we resume; the response payload itself doesn't carry a secret —
        // it's just the signal that consent completed.
        response: 'consented',
      })),
    };

    try {
      await this.chatHttpService.sendChatRequest(resumeRequest);
    } catch (error) {
      this.chatStateService.setChatLoading(false);
      this.messageMapService.endStreaming();

      // 400 from the resume route means the agent's `_interrupt_state` no
      // longer holds the submitted ids — the cache evicted, the pod
      // restarted, or the breadcrumb outlived its agent. Surface a
      // conversational error so the user knows to retry the prompt
      // instead of staring at a stuck spinner.
      if (this.isExpiredInterruptError(error)) {
        this.errorService.addError(
          'Authorization expired',
          'The agent paused too long ago to resume this turn automatically. Please send your message again.',
        );
        return;
      }
      throw error;
    }
  }

  /** Detect the 400 the inference-api returns for unknown/expired interrupt
   *  ids. Both fetch-based and HttpClient-based flows are checked because
   *  the resume path uses `fetch-event-source`, which surfaces errors as
   *  plain Error/Response objects rather than HttpErrorResponse. */
  private isExpiredInterruptError(error: unknown): boolean {
    if (error instanceof HttpErrorResponse) {
      return error.status === 400;
    }
    if (typeof error === 'object' && error !== null) {
      const status = (error as { status?: unknown }).status;
      if (status === 400) return true;
      const message = (error as { message?: unknown }).message;
      if (typeof message === 'string' && /expired interrupt/i.test(message)) {
        return true;
      }
    }
    return false;
  }

  /**
   * Get file attachment metadata for display in user messages.
   * Retrieves file metadata from FileUploadService for given upload IDs.
   */
  private getFileAttachments(fileUploadIds?: string[]): FileAttachmentData[] | undefined {
    if (!fileUploadIds || fileUploadIds.length === 0) {
      return undefined;
    }

    const attachments: FileAttachmentData[] = [];

    for (const uploadId of fileUploadIds) {
      // Get file metadata from the upload service
      const fileMeta = this.fileUploadService.getReadyFileById(uploadId);
      if (fileMeta) {
        attachments.push({
          uploadId: fileMeta.uploadId,
          filename: fileMeta.filename,
          mimeType: fileMeta.mimeType,
          sizeBytes: fileMeta.sizeBytes,
        });
      }
    }

    return attachments.length > 0 ? attachments : undefined;
  }
}
