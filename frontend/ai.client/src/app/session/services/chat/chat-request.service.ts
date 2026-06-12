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
import { SkillService } from '../../../services/skill/skill.service';
import { ChatModeService } from '../../../services/chat-mode/chat-mode.service';
import { FileUploadService } from '../../../services/file-upload';
import { FileAttachmentData } from '../models/message.model';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import {
  ToolApprovalDecision,
  ToolApprovalService,
} from '../../../services/tool-approval/tool-approval.service';
import { ErrorService } from '../../../services/error/error.service';
import { StreamParserService } from './stream-parser.service';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';
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
  private skillService = inject(SkillService);
  private chatModeService = inject(ChatModeService);
  private fileUploadService = inject(FileUploadService);
  private oauthConsentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);
  private streamParserService = inject(StreamParserService);
  private errorService = inject(ErrorService);
  private systemPromptsService = inject(SystemPromptsService);
  private router = inject(Router);
  // TODO: Inject proper logging service

  constructor() {
    this.oauthConsentService.setResumeHandler((interruptIds, context) =>
      this.resumeFromOAuthConsent(interruptIds, context?.sessionId),
    );
    this.toolApprovalService.setResumeHandler((interruptId, decision, context) =>
      this.resumeFromToolApproval(interruptId, decision, context?.sessionId),
    );
  }

  ngOnDestroy(): void {
    this.oauthConsentService.setResumeHandler(null);
    this.toolApprovalService.setResumeHandler(null);
  }

  async submitChatRequest(
    userInput: string,
    sessionId: string | null,
    fileUploadIds?: string[],
    assistantId?: string,
  ): Promise<void> {
    // Ensure conversation exists and get its ID
    // Update URL to reflect current conversation
    // Any new send (including a "Continue") retires the previous turn's
    // max_tokens "Continue" affordance immediately, before the stream starts.
    this.chatStateService.setLastTurnContinuable(false);

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

      // If the user picked a conversation mode on the home page (before
      // any session existed), claim it for this new session so the
      // metadata-arrival effect doesn't wipe it.
      this.systemPromptsService.bindToSession(sessionId);
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
   * "Continue" after a max_tokens truncation. Modeled on the OAuth/tool
   * resume flow, NOT on submitChatRequest: no user message is added (no
   * visible bubble, no new user turn) so the model resumes the truncated
   * assistant message in restored history instead of answering a fresh
   * instruction. The full request object is sent so the backend rebuilds
   * the same agent shape; `continue_truncated` makes it re-enter the loop
   * with an empty prompt.
   */
  async continueTruncatedTurn(
    sessionId: string | null,
    assistantId?: string,
  ): Promise<void> {
    if (!sessionId) {
      return;
    }

    // Hide the affordance immediately; retire any stale continuable state.
    this.chatStateService.setLastTurnContinuable(false);

    // Continuation streaming: pins the existing messages (history +
    // truncated partial + error bubble) as a stable prefix and appends the
    // continuation after them, instead of the normal sync which would
    // truncate back to the last user message and drop the partial. Also
    // resets the parser (with the correct starting count) so the resumed
    // stream is treated as a fresh batch.
    this.messageMapService.beginContinuationStreaming(sessionId);
    this.chatStateService.createNewAbortController();
    this.chatStateService.setChatLoading(true);

    // Reuse the normal request shape so the backend rebuilds the same
    // model/tools/assistant agent, but with an empty message and the
    // continuation flag. No addUserMessage call → no user bubble.
    const requestObject = this.buildChatRequestObject('', sessionId, undefined, assistantId);
    requestObject['message'] = '';
    requestObject['continue_truncated'] = true;

    try {
      await this.chatHttpService.sendChatRequest(requestObject);
    } catch (error) {
      this.chatStateService.setChatLoading(false);
      this.messageMapService.endStreaming();
      throw error;
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

    // Skills mode vs. tools mode (assistant turns are excluded below — they
    // keep their pre-skills-mode behavior and let the server default apply).
    const chatMode = this.chatModeService.mode();
    const isSkillsMode = !assistantId && chatMode === 'skill';

    // In skills mode capabilities come exclusively from the enabled skills'
    // bound tools — the raw tool selection is intentionally not sent.
    const enabledTools = isSkillsMode ? [] : this.toolService.getEnabledToolIds();

    const requestObject: Record<string, unknown> = {
      message,
      session_id,
      model_id: isDefaultModel ? null : selectedModel.modelId,
      enabled_tools: enabledTools,
      provider: isDefaultModel ? null : selectedModel.provider,
    };

    if (!assistantId) {
      // Pin the turn to the user's mode. The server honors this only while
      // the admin policy allows toggling; otherwise it enforces the default.
      requestObject['agent_type'] = chatMode;
      if (isSkillsMode) {
        requestObject['enabled_skills'] = this.skillService.getEnabledSkillIds();
      }
    }

    // Per-model inference param overrides set in the Settings → Advanced
    // panel. Backend layers these on top of admin defaults and clamps to the
    // model's bounds; locked params drop the override silently.
    if (!isDefaultModel) {
      const overrides = this.modelService.getInferenceParamOverrides();
      if (Object.keys(overrides).length > 0) {
        requestObject['inference_params'] = overrides;
      }
    }

    // Add file upload IDs if present
    if (fileUploadIds && fileUploadIds.length > 0) {
      requestObject['file_upload_ids'] = fileUploadIds;
    }

    // Add assistant ID if present
    // NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
    // AgentCore Runtime's internal 'assistant_id' field handling (causes 424 error)
    if (assistantId) {
      requestObject['rag_assistant_id'] = assistantId;
      // Assistants are KB-grounded with no external tools. Backend enforces this too.
      requestObject['enabled_tools'] = [];
    } else {
      // Forward the active conversation mode for non-assistant turns. The
      // assistant path is intentionally excluded server-side too — assistants
      // are KB-grounded and a "mode" prompt could contradict the assistant's
      // own instructions. Sending the id every turn lets the inference path
      // resolve the prompt without round-tripping session metadata, which
      // matters on the first turn of a brand-new session (no metadata row
      // exists yet).
      const activePromptId = this.systemPromptsService.activePromptId();
      if (activePromptId) {
        requestObject['selected_prompt_id'] = activePromptId;
      }
    }

    return requestObject;
  }

  /**
   * Resume the paused agent turn by POSTing the interrupt responses. The
   * backend rebuilds the agent from its persisted ``PausedTurnSnapshot``,
   * so this request only needs to identify the session and the interrupts —
   * no model / tools / prompt context is sent or required. Triggered by
   * OAuthConsentService after the user completes a consent popup.
   */
  private async resumeFromOAuthConsent(
    interruptIds: string[],
    sessionId?: string,
  ): Promise<void> {
    if (interruptIds.length === 0 || !sessionId) {
      return;
    }

    // Reset the parser so the resumed stream is treated as a fresh batch
    // of events. Without this, the parser stays in Completed state from
    // the prior `done` and ignores everything.
    this.streamParserService.reset(sessionId);
    this.messageMapService.startStreaming(sessionId);
    this.chatStateService.createNewAbortController();
    this.chatStateService.setChatLoading(true);

    const resumeRequest: Record<string, unknown> = {
      session_id: sessionId,
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

      // 400 from the resume route means either the persisted snapshot is
      // missing/expired, or the agent's `_interrupt_state` doesn't recognize
      // the submitted ids. Either way the user needs to retry the prompt.
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

  /**
   * Resume the paused agent turn after the user approves or declines a
   * flagged MCP tool call. The hook on the backend reads the response
   * string ("approved" / "declined") and either lets the tool proceed or
   * cancels it.
   */
  private async resumeFromToolApproval(
    interruptId: string,
    decision: ToolApprovalDecision,
    sessionId?: string,
  ): Promise<void> {
    if (!sessionId) {
      return;
    }

    this.streamParserService.reset(sessionId);
    this.messageMapService.startStreaming(sessionId);
    this.chatStateService.createNewAbortController();
    this.chatStateService.setChatLoading(true);

    const resumeRequest: Record<string, unknown> = {
      session_id: sessionId,
      message: '',
      interrupt_responses: [
        {
          interruptId,
          response: decision,
        },
      ],
    };

    try {
      await this.chatHttpService.sendChatRequest(resumeRequest);
    } catch (error) {
      this.chatStateService.setChatLoading(false);
      this.messageMapService.endStreaming();

      if (this.isExpiredInterruptError(error)) {
        this.errorService.addError(
          'Approval expired',
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
