import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { ChatRequestService } from './chat-request.service';
import { ChatHttpService } from './chat-http.service';
import { ChatStateService } from './chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { UserService } from '../../../auth/user.service';
import { ModelService } from '../model/model.service';
import { ToolService } from '../../../services/tool/tool.service';
import { SkillService } from '../../../services/skill/skill.service';
import { ChatModeService, ChatMode } from '../../../services/chat-mode/chat-mode.service';
import { FileUploadService } from '../../../services/file-upload';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';

describe('ChatRequestService', () => {
  let service: ChatRequestService;
  let mockChatHttpService: any;
  let mockRouter: any;
  let mockModelService: any;
  let mockToolService: any;
  let currentMode: ChatMode;
  // Captured from the constructor's setResumeHandler(...) calls so the tests
  // can drive the (private) resume paths the way the consent/approval UIs do.
  let oauthResumeHandler: ((interruptIds: string[], context?: { sessionId?: string }) => Promise<void>) | null;
  let approvalResumeHandler: ((interruptId: string, decision: any, context?: { sessionId?: string }) => Promise<void>) | null;

  beforeEach(() => {
    TestBed.resetTestingModule();
    currentMode = 'skill';
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
    };

    TestBed.configureTestingModule({
      providers: [
        ChatRequestService,
        { provide: ChatHttpService, useValue: mockChatHttpService },
        { provide: Router, useValue: mockRouter },
        { provide: ChatStateService, useValue: { setChatLoading: vi.fn(), setLastTurnContinuable: vi.fn(), createNewAbortController: vi.fn() } },
        { provide: MessageMapService, useValue: { addUserMessage: vi.fn(), startStreaming: vi.fn(), beginContinuationStreaming: vi.fn(), endStreaming: vi.fn(), reloadMessagesForSession: vi.fn().mockResolvedValue(undefined) } },
        { provide: SessionService, useValue: { addSessionToCache: vi.fn() } },
        { provide: UserService, useValue: { getUser: vi.fn().mockReturnValue({ user_id: 'user1' }) } },
        { provide: ModelService, useValue: mockModelService },
        { provide: ToolService, useValue: mockToolService },
        { provide: SkillService, useValue: { getEnabledSkillIds: vi.fn().mockReturnValue(['skill_a']) } },
        { provide: ChatModeService, useValue: { mode: () => currentMode } },
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

  it('should submit chat request with existing session (skills mode)', async () => {
    await service.submitChatRequest('Hello', 'session1');

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'Hello',
        session_id: 'session1',
        model_id: 'test-model',
        provider: 'test',
        agent_type: 'skill',
        enabled_skills: ['skill_a'],
        // Skills mode: capabilities come from skills, not the tool picker.
        enabled_tools: [],
      })
    );
  });

  it('should submit chat request with new session (skills mode)', async () => {
    await service.submitChatRequest('Hello', null);

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'Hello',
        model_id: 'test-model',
        provider: 'test',
        agent_type: 'skill',
        enabled_skills: ['skill_a'],
        enabled_tools: [],
      })
    );
  });

  it('sends the tool selection and no skills in tools mode', async () => {
    currentMode = 'chat';
    await service.submitChatRequest('Hello', 'session1');

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        agent_type: 'chat',
        enabled_tools: ['tool1', 'tool2'],
      })
    );
    const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
    expect('enabled_skills' in sent).toBe(false);
  });

  it('assistant turns carry no agent_type or enabled_skills (pre-skills-mode behavior)', async () => {
    await service.submitChatRequest('Hello', 'session1', undefined, 'assistant1');

    const sent = mockChatHttpService.sendChatRequest.mock.calls[0][0];
    expect('agent_type' in sent).toBe(false);
    expect('enabled_skills' in sent).toBe(false);
    // Assistants forward the user's tool selection (skills mode is excluded for
    // assistant turns), so the raw tool picker selection rides along.
    expect(sent['enabled_tools']).toEqual(['tool1', 'tool2']);
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

  it('should throw error when no model selected', async () => {
    mockModelService.getSelectedModel.mockReturnValue(null);

    await expect(service.submitChatRequest('Hello', 'session1')).rejects.toThrow(
      'No model selected. Please select a model before sending a message.'
    );
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
});