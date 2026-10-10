import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { ChatRequestService } from './chat-request.service';
import { ChatHttpService, StreamNotStartedError } from './chat-http.service';
import { FailedSendService } from './failed-send.service';
import { ChatStateService } from './chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { MessageFeedbackService } from '../session/message-feedback.service';
import { SessionService } from '../session/session.service';
import { SessionPrewarmService } from '../session/session-prewarm.service';
import { UserService } from '../../../auth/user.service';
import { ModelService } from '../model/model.service';
import { ToolService } from '../../../services/tool/tool.service';
import { SkillService } from '../../../services/skill/skill.service';
import { FileUploadService } from '../../../services/file-upload';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';

describe('ChatRequestService', () => {
  let service: ChatRequestService;
  let mockChatHttpService: any;
  let mockRouter: any;
  let mockModelService: any;
  let mockToolService: any;
  let mockSkillService: any;
  // Captured from the constructor's setResumeHandler(...) calls so the tests
  // can drive the (private) resume paths the way the consent/approval UIs do.
  let oauthResumeHandler: ((interruptIds: string[], context?: { sessionId?: string }) => Promise<void>) | null;
  let approvalResumeHandler: ((interruptId: string, decision: any, context?: { sessionId?: string }) => Promise<void>) | null;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockChatHttpService = {
      sendChatRequest: vi.fn().mockResolvedValue(undefined),
    };

    mockRouter = {
      navigate: vi.fn(),
    };

    mockModelService = {
      getSelectedModel: vi.fn().mockReturnValue({ modelId: 'test-model', provider: 'test' }),
      isUsingDefaultModel: vi.fn().mockReturnValue(false),
      getInferenceParamOverrides: vi.fn().mockReturnValue({}),
    };

    mockToolService = {
      getEnabledToolIds: vi.fn().mockReturnValue(['tool1', 'tool2']),
      // Already loaded, which is the steady state from turn 2 onwards.
      ensureLoaded: vi.fn().mockResolvedValue(undefined),
    };

    // Default: nothing selected in the skills picker (D6 opt-in), which is the
    // state for a user who never opens it.
    mockSkillService = {
      getEnabledSkillIds: vi.fn().mockReturnValue([]),
      ensureLoaded: vi.fn().mockResolvedValue(undefined),
    };

    TestBed.configureTestingModule({
      providers: [
        ChatRequestService,
        { provide: ChatHttpService, useValue: mockChatHttpService },
        { provide: Router, useValue: mockRouter },
        { provide: ChatStateService, useValue: { setChatLoading: vi.fn(), setLastTurnContinuable: vi.fn(), setLastTurnInterrupted: vi.fn(), setViewedSession: vi.fn(), isSessionLoading: vi.fn().mockReturnValue(false) } },
        { provide: MessageMapService, useValue: { addUserMessage: vi.fn(), startStreaming: vi.fn(), beginContinuationStreaming: vi.fn(), endStreaming: vi.fn(), reloadMessagesForSession: vi.fn().mockResolvedValue(undefined), markSendFailed: vi.fn(), removeTrailingMessage: vi.fn().mockReturnValue(true) } },
        { provide: SessionService, useValue: { addSessionToCache: vi.fn() } },
        { provide: MessageFeedbackService, useValue: { consumePendingRetry: vi.fn() } },
        { provide: UserService, useValue: { getUser: vi.fn().mockReturnValue({ user_id: 'user1' }) } },
        { provide: ModelService, useValue: mockModelService },
        { provide: ToolService, useValue: mockToolService },
        { provide: SkillService, useValue: mockSkillService },
        { provide: FileUploadService, useValue: { getReadyFileById: vi.fn() } },
        {
          provide: OAuthConsentService,
          useValue: {
            setResumeHandler: vi.fn((handler: any) => {
              oauthResumeHandler = handler;
            }),
          },
        },
        {
          provide: ToolApprovalService,
          useValue: {
            setResumeHandler: vi.fn((handler: any) => {
              approvalResumeHandler = handler;
            }),
          },
        },
      ],
    });
    service = TestBed.inject(ChatRequestService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('should submit chat request with existing session', async () => {
    await service.submitChatRequest('Hello', 'session1');

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'Hello',
        session_id: 'session1',
        model_id: 'test-model',
        provider: 'test',
        // Always forward the tool selection; skills-mode plumbing is removed.
        enabled_tools: ['tool1', 'tool2'],
      })
    );
  });

  it('should submit chat request with new session', async () => {
    await service.submitChatRequest('Hello', null);

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'Hello',
        model_id: 'test-model',
        provider: 'test',
        enabled_tools: ['tool1', 'tool2'],
      })
    );
  });

  describe('session prewarm', () => {
    it('a new conversation starts on the id the page warmed, registered like any new one', async () => {
      const prewarm = TestBed.inject(SessionPrewarmService);
      vi.spyOn(prewarm, 'claimNewConversationId').mockReturnValue('warmed-id');
      const sessions = TestBed.inject(SessionService) as unknown as { addSessionToCache: ReturnType<typeof vi.fn> };

      await service.submitChatRequest('Hello', null);

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({ session_id: 'warmed-id' }),
      );
      // Still treated as new: registered in the sidebar cache before navigating.
      expect(sessions.addSessionToCache).toHaveBeenCalledWith('warmed-id', 'user1');
    });

    it('mints its own id when nothing was warmed (the flag is off)', async () => {
      const prewarm = TestBed.inject(SessionPrewarmService);
      vi.spyOn(prewarm, 'claimNewConversationId').mockReturnValue(null);

      await service.submitChatRequest('Hello', null);

      const sent = mockChatHttpService.sendChatRequest.mock.calls.at(-1)?.[0];
      expect(sent.session_id).toMatch(/^[0-9a-f-]{36}$/);
    });

    it('an existing conversation never claims the warmed id', async () => {
      const prewarm = TestBed.inject(SessionPrewarmService);
      const claim = vi.spyOn(prewarm, 'claimNewConversationId');

      await service.submitChatRequest('Hello', 'session1');

      expect(claim).not.toHaveBeenCalled();
    });
  });

  it('never sends agent_type (skills mode removed)', async () => {
    await service.submitChatRequest('Hello', 'session1');

    const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
    expect('agent_type' in sent).toBe(false);
  });

  it('omits enabled_skills entirely when nothing is selected (D6 opt-in)', async () => {
    await service.submitChatRequest('Hello', 'session1');

    // Omission, not `[]`: the backend already reads absent as "no skills", so a
    // turn from a user who never touched the picker is byte-identical to a
    // pre-skills turn.
    const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
    expect('enabled_skills' in sent).toBe(false);
  });

  it('sends the picker selection as enabled_skills', async () => {
    mockSkillService.getEnabledSkillIds.mockReturnValue(['web_research']);

    await service.submitChatRequest('Hello', 'session1');

    const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
    expect(sent['enabled_skills']).toEqual(['web_research']);
    // Skills ride a plain chat turn — there is no separate skills mode.
    expect('agent_type' in sent).toBe(false);
  });

  it('assistant turns carry the skill selection too', async () => {
    mockSkillService.getEnabledSkillIds.mockReturnValue(['web_research']);

    await service.submitChatRequest('Hello', 'session1', undefined, 'assistant1');

    const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
    expect('agent_type' in sent).toBe(false);
    expect(sent['enabled_skills']).toEqual(['web_research']);
    // Assistants forward the user's tool selection, so it rides along.
    expect(sent['enabled_tools']).toEqual(['tool1', 'tool2']);
  });

  // ── The first-turn race (#1160) ────────────────────────────────────────────────
  //
  // A message sent seconds after page load used to be assembled from tool and
  // skill lists that had not arrived yet: turn 1 disclosed no skills and no
  // tools, turn 2 disclosed the real ones, and the whole cacheable prefix
  // (system prompt + toolConfig) was re-written on turn 2 at the cache-write
  // premium. The send now waits for both lists to settle.
  describe('selection sources not yet loaded', () => {
    /** A load that only completes when the test says so. */
    function deferred() {
      let release!: () => void;
      const promise = new Promise<void>(resolve => {
        release = resolve;
      });
      return { promise, release };
    }

    it('a message submitted before the skills load completes still carries the skills', async () => {
      const load = deferred();
      // Until the load lands the picker answers "nothing enabled", which is
      // indistinguishable from a user who turned everything off.
      mockSkillService.getEnabledSkillIds.mockReturnValue([]);
      mockSkillService.ensureLoaded.mockReturnValue(
        load.promise.then(() => {
          mockSkillService.getEnabledSkillIds.mockReturnValue([
            'pdf_workflows',
            'web_research',
          ]);
        }),
      );

      const submitted = service.submitChatRequest('Hello', null);

      // The request must not have gone out on the empty list.
      await Promise.resolve();
      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();

      load.release();
      await submitted;

      const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
      expect(sent['enabled_skills']).toEqual(['pdf_workflows', 'web_research']);
    });

    it('a message submitted before the tools load completes still carries the tools', async () => {
      const load = deferred();
      mockToolService.getEnabledToolIds.mockReturnValue([]);
      mockToolService.ensureLoaded.mockReturnValue(
        load.promise.then(() => {
          mockToolService.getEnabledToolIds.mockReturnValue(['tool1', 'tool2']);
        }),
      );

      const submitted = service.submitChatRequest('Hello', null);
      await Promise.resolve();
      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();

      load.release();
      await submitted;

      const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
      expect(sent['enabled_tools']).toEqual(['tool1', 'tool2']);
    });

    it('shows the user message immediately — the wait is behind the optimistic UI', async () => {
      const load = deferred();
      mockSkillService.ensureLoaded.mockReturnValue(load.promise);

      const submitted = service.submitChatRequest('Hello', null);
      await Promise.resolve();

      const messageMap = TestBed.inject(MessageMapService) as any;
      expect(messageMap.addUserMessage).toHaveBeenCalled();
      expect(messageMap.startStreaming).toHaveBeenCalled();

      load.release();
      await submitted;
    });

    it('sends anyway when a load never returns, rather than swallowing the message', async () => {
      vi.useFakeTimers();
      try {
        // Never resolves: a hung /skills/ request must not hold the turn.
        mockSkillService.ensureLoaded.mockReturnValue(new Promise<void>(() => undefined));

        const submitted = service.submitChatRequest('Hello', null);
        await vi.advanceTimersByTimeAsync(5000);
        await submitted;

        expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('waits for the selections on a Continue too, so it rebuilds the same shape', async () => {
      const load = deferred();
      mockSkillService.getEnabledSkillIds.mockReturnValue([]);
      mockSkillService.ensureLoaded.mockReturnValue(
        load.promise.then(() => {
          mockSkillService.getEnabledSkillIds.mockReturnValue(['pdf_workflows']);
        }),
      );

      const submitted = service.continueTruncatedTurn('session1');
      await Promise.resolve();
      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();

      load.release();
      await submitted;

      const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
      expect(sent['continue_truncated']).toBe(true);
      expect(sent['enabled_skills']).toEqual(['pdf_workflows']);
    });
  });

  it('should include assistant ID in request', async () => {
    await service.submitChatRequest('Hello', 'session1', undefined, 'assistant1');

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        rag_assistant_id: 'assistant1',
      })
    );
  });

  it('forwards the user tool selection when an assistant ID is set (assistants can use tools)', async () => {
    await service.submitChatRequest('Hello', 'session1', undefined, 'assistant1');

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        rag_assistant_id: 'assistant1',
        enabled_tools: ['tool1', 'tool2'],
      })
    );
  });

  // ── `@`-mention: one turn, not a binding (Marketplace D11) ──────────────────────
  describe('agent mention', () => {
    it('sends the mentioned agent with the flag that stops it binding the session', async () => {
      await service.submitChatRequest('@Policy Lookup hi', 'session1', undefined, undefined, 'agent-9');

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          rag_assistant_id: 'agent-9',
          agent_mention: true,
        }),
      );
    });

    it('a mention beats the conversation-bound assistant for that turn', async () => {
      // The user just named who they want, in the composer, for this message.
      await service.submitChatRequest('@Grader hi', 'session1', undefined, 'assistant1', 'agent-9');

      const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
      expect(sent['rag_assistant_id']).toBe('agent-9');
      expect(sent['agent_mention']).toBe(true);
    });

    it('leaves the URL bound to the conversation assistant, not the mention', async () => {
      // Otherwise one `@` would convert the whole thread — the SPA reads this param as
      // the session's assistant on every subsequent load.
      await service.submitChatRequest('@Grader hi', null, undefined, 'assistant1', 'agent-9');

      const navigation = mockRouter.navigate.mock.calls[0];
      expect(navigation[1].queryParams).toEqual({ assistantId: 'assistant1' });
    });

    it('an unmentioned turn carries no flag at all', async () => {
      await service.submitChatRequest('Hello', 'session1', undefined, 'assistant1');

      const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
      expect('agent_mention' in sent).toBe(false);
    });
  });

  it('keys loading and viewed-session state to the submitted session', async () => {
    const chatState = TestBed.inject(ChatStateService) as any;

    await service.submitChatRequest('Hello', 'session1');

    expect(chatState.setViewedSession).toHaveBeenCalledWith('session1');
    expect(chatState.setChatLoading).toHaveBeenCalledWith('session1', true);
    expect(chatState.setLastTurnContinuable).toHaveBeenCalledWith('session1', false);
  });

  it('should throw error when no model selected', async () => {
    mockModelService.getSelectedModel.mockReturnValue(null);

    await expect(service.submitChatRequest('Hello', 'session1')).rejects.toThrow(
      'No model selected. Please select a model before sending a message.'
    );
  });

  describe('a send refused before anything streamed', () => {
    // Dev, 2026-10-09: a queued follow-up flushed into a Runtime 403 left a
    // bubble that looked delivered, with no reply and no way to resend it.

    beforeEach(() => {
      const messageMap = TestBed.inject(MessageMapService) as any;
      messageMap.addUserMessage.mockReturnValue({ id: 'msg-session1-6', role: 'user', content: [] });
    });

    it('marks the message not sent, with the reason, and still rejects', async () => {
      mockChatHttpService.sendChatRequest.mockRejectedValueOnce(
        new StreamNotStartedError('Your sign-in was being renewed when this was sent.', 403),
      );

      await expect(service.submitChatRequest('Hello', 'session1')).rejects.toBeInstanceOf(
        StreamNotStartedError,
      );

      const messageMap = TestBed.inject(MessageMapService) as any;
      expect(messageMap.markSendFailed).toHaveBeenCalledWith(
        'session1',
        'msg-session1-6',
        'Your sign-in was being renewed when this was sent.',
      );
      expect(TestBed.inject(FailedSendService).canRetry('msg-session1-6')).toBe(true);
    });

    it('a retry drops the failed bubble and resends the same message', async () => {
      mockChatHttpService.sendChatRequest.mockRejectedValueOnce(
        new StreamNotStartedError('A response was still finishing for this conversation.', 409),
      );
      await service
        .submitChatRequest('Hello', 'session1', undefined, 'ast-1', undefined, ['skill-a'])
        .catch(() => undefined);

      TestBed.inject(FailedSendService).retry('msg-session1-6');
      await vi.waitFor(() => expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledTimes(2));

      const messageMap = TestBed.inject(MessageMapService) as any;
      expect(messageMap.removeTrailingMessage).toHaveBeenCalledWith('session1', 'msg-session1-6');
      expect(mockChatHttpService.sendChatRequest.mock.calls[1][0]).toMatchObject({
        message: 'Hello',
        session_id: 'session1',
        rag_assistant_id: 'ast-1',
        invoked_skills: ['skill-a'],
      });
      // One retry per failure: the handle is spent.
      expect(TestBed.inject(FailedSendService).canRetry('msg-session1-6')).toBe(false);
    });

    it('a failure after the stream opened is not marked — the agent saw the message', async () => {
      mockChatHttpService.sendChatRequest.mockRejectedValueOnce(new Error('network dropped'));

      await expect(service.submitChatRequest('Hello', 'session1')).rejects.toThrow('network dropped');

      const messageMap = TestBed.inject(MessageMapService) as any;
      expect(messageMap.markSendFailed).not.toHaveBeenCalled();
    });
  });

  describe('continueTruncatedTurn', () => {
    it('sends continue_truncated with an empty message', async () => {
      await service.continueTruncatedTurn('session1', 'assistant1');

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '',
          session_id: 'session1',
          continue_truncated: true,
          rag_assistant_id: 'assistant1',
        }),
      );
    });

    it('does NOT add a user message (no visible bubble); uses continuation streaming', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;
      await service.continueTruncatedTurn('session1');

      expect(messageMap.addUserMessage).not.toHaveBeenCalled();
      expect(messageMap.startStreaming).not.toHaveBeenCalled();
      expect(messageMap.beginContinuationStreaming).toHaveBeenCalledWith('session1');
    });

    it('is a no-op without a session id', async () => {
      await service.continueTruncatedTurn(null);
      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();
    });
  });

  describe('resumeFromOAuthConsent', () => {
    it('pins existing messages (continuation streaming) and reconciles from server', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;

      await oauthResumeHandler!(['int-1'], { sessionId: 'session1' });

      // The paused tool card must NOT be truncated away: continuation
      // streaming pins it as a prefix instead of the normal truncate-to-user sync.
      expect(messageMap.beginContinuationStreaming).toHaveBeenCalledWith('session1');
      expect(messageMap.startStreaming).not.toHaveBeenCalled();
      // No new user bubble on a resume turn.
      expect(messageMap.addUserMessage).not.toHaveBeenCalled();

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          session_id: 'session1',
          message: '',
          interrupt_responses: [{ interruptId: 'int-1', response: 'consented' }],
        }),
      );

      // The resumed stream can't attach the tool_result live, so we
      // reconcile from persisted memory to flip the card to its result.
      expect(messageMap.reloadMessagesForSession).toHaveBeenCalledWith('session1');
    });

    it('is a no-op without interrupt ids', async () => {
      await oauthResumeHandler!([], { sessionId: 'session1' });
      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();
    });

    it('is a no-op without a session id', async () => {
      await oauthResumeHandler!(['int-1'], {});
      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();
    });
  });

  describe('resumeFromToolApproval', () => {
    it('pins existing messages and reconciles from server after a decision', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;

      await approvalResumeHandler!('int-9', 'approved', { sessionId: 'session1' });

      expect(messageMap.beginContinuationStreaming).toHaveBeenCalledWith('session1');
      expect(messageMap.startStreaming).not.toHaveBeenCalled();
      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          session_id: 'session1',
          message: '',
          interrupt_responses: [{ interruptId: 'int-9', response: 'approved' }],
        }),
      );
      expect(messageMap.reloadMessagesForSession).toHaveBeenCalledWith('session1');
    });
  });

  /**
   * Preview turns (agent designer, marketplace review test drive) run on this
   * service rather than on a parallel implementation. The fork they replaced
   * handled 9 of the ~27 SSE events the shared parser dispatches, and because
   * every parser callback is optional, the ones it skipped — including
   * `tool_approval_required` and `oauth_required` — were dropped in silence. A
   * tool call that paused for approval was therefore never surfaced, never
   * answered, and never dispatched. These tests pin the shape that keeps the
   * preview on the one code path everyone else exercises.
   */
  describe('submitPreviewRequest', () => {
    const preview = {
      sessionId: 'preview-abc',
      agentId: 'ast-001',
      message: 'hello',
    };

    it('routes through the same transport as a real chat turn', async () => {
      await service.submitPreviewRequest(preview);

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          message: 'hello',
          session_id: 'preview-abc',
          rag_assistant_id: 'ast-001',
          model_id: null,
        }),
      );
    });

    /**
     * An Agent resolves instructions, model, tools, skills and memory server-side
     * from its own record. Sending the viewer's selections would fight the
     * bindings and test a shape nobody will ever run — and a long persona sent as
     * `system_prompt` exceeds the length cap outright (422).
     */
    it('sends no prompt, model, tool or skill selection of its own', async () => {
      await service.submitPreviewRequest(preview);

      const body = mockChatHttpService.sendChatRequest.mock.calls[0][0];
      expect(body).not.toHaveProperty('system_prompt');
      expect(body).not.toHaveProperty('enabled_tools');
      expect(body).not.toHaveProperty('enabled_skills');
      expect(body).not.toHaveProperty('provider');
      expect(body).not.toHaveProperty('selected_prompt_id');
      expect(mockToolService.getEnabledToolIds).not.toHaveBeenCalled();
    });

    /** The preview is embedded in the editor; routing away would eject the user. */
    it('does not navigate or register the session in the sidenav', async () => {
      const sessionService = TestBed.inject(SessionService) as any;

      await service.submitPreviewRequest(preview);

      expect(mockRouter.navigate).not.toHaveBeenCalled();
      expect(sessionService.addSessionToCache).not.toHaveBeenCalled();
    });

    /**
     * The viewed-session facades drive the MAIN composer's spinner and cost badge.
     * A preview claiming them would freeze the real chat's input behind a stream
     * happening in a side panel.
     */
    it('keys loading to the preview session without claiming the viewed session', async () => {
      const chatState = TestBed.inject(ChatStateService) as any;

      await service.submitPreviewRequest(preview);

      expect(chatState.setChatLoading).toHaveBeenCalledWith('preview-abc', true);
      expect(chatState.setViewedSession).not.toHaveBeenCalled();
    });

    it('adds the user message and starts streaming on the preview session', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;

      await service.submitPreviewRequest(preview);

      expect(messageMap.addUserMessage).toHaveBeenCalledWith('preview-abc', 'hello', undefined);
      expect(messageMap.startStreaming).toHaveBeenCalledWith('preview-abc');
    });

    it('offers the added user message to the feedback service as a possible retry', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;
      const feedback = TestBed.inject(MessageFeedbackService) as any;
      const added = { id: 'msg-preview-abc-2', role: 'user', content: [] };
      messageMap.addUserMessage.mockReturnValue(added);

      await service.submitPreviewRequest(preview);

      expect(feedback.consumePendingRetry).toHaveBeenCalledWith('preview-abc', added);
    });

    it('forwards file uploads', async () => {
      await service.submitPreviewRequest({ ...preview, fileUploadIds: ['up-1'] });

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({ file_upload_ids: ['up-1'] }),
      );
    });

    /**
     * Omitted rather than sent as `false`: the invocation path refuses a caller
     * without `admin.marketplace` outright rather than downgrading them, so an
     * ordinary author's request must carry no claim about the scope at all.
     */
    it('omits review_preview entirely unless the caller is a reviewer', async () => {
      await service.submitPreviewRequest(preview);
      expect(mockChatHttpService.sendChatRequest.mock.calls[0][0]).not.toHaveProperty(
        'review_preview',
      );

      mockChatHttpService.sendChatRequest.mockClear();
      await service.submitPreviewRequest({ ...preview, reviewPreview: true });
      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({ review_preview: true }),
      );
    });

    it('is a no-op for empty content or a draft agent with no id', async () => {
      await service.submitPreviewRequest({ ...preview, message: '   ' });
      await service.submitPreviewRequest({ ...preview, agentId: '' });

      expect(mockChatHttpService.sendChatRequest).not.toHaveBeenCalled();
    });
  });

  /**
   * In-memory resume for preview sessions.
   *
   * The backend skips its metadata writes for `preview-` ids — no
   * `PendingInterrupt`, no `PausedTurnSnapshot` — so the live parser's state is
   * the only record of the turn. Reloading from the server here would fetch a
   * session that was never written and replace a correct transcript with an
   * empty one.
   */
  describe('resume on a preview session', () => {
    it('resumes an approval without reloading from a session that was never persisted', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;

      await approvalResumeHandler!('int-1', 'approved', { sessionId: 'preview-abc' });

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          session_id: 'preview-abc',
          interrupt_responses: [{ interruptId: 'int-1', response: 'approved' }],
        }),
      );
      expect(messageMap.reloadMessagesForSession).not.toHaveBeenCalled();
    });

    it('resumes an OAuth consent the same way', async () => {
      const messageMap = TestBed.inject(MessageMapService) as any;

      await oauthResumeHandler!(['int-2'], { sessionId: 'preview-abc' });

      expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
        expect.objectContaining({ session_id: 'preview-abc' }),
      );
      expect(messageMap.reloadMessagesForSession).not.toHaveBeenCalled();
    });
  });
});