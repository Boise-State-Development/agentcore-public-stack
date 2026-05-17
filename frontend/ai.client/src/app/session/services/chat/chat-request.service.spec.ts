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
import { FileUploadService } from '../../../services/file-upload';

describe('ChatRequestService', () => {
  let service: ChatRequestService;
  let mockChatHttpService: any;
  let mockRouter: any;
  let mockModelService: any;
  let mockToolService: any;

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
    };

    TestBed.configureTestingModule({
      providers: [
        ChatRequestService,
        { provide: ChatHttpService, useValue: mockChatHttpService },
        { provide: Router, useValue: mockRouter },
        { provide: ChatStateService, useValue: { setChatLoading: vi.fn(), setLastTurnContinuable: vi.fn(), createNewAbortController: vi.fn() } },
        { provide: MessageMapService, useValue: { addUserMessage: vi.fn(), startStreaming: vi.fn(), beginContinuationStreaming: vi.fn(), endStreaming: vi.fn() } },
        { provide: SessionService, useValue: { addSessionToCache: vi.fn() } },
        { provide: UserService, useValue: { getUser: vi.fn().mockReturnValue({ user_id: 'user1' }) } },
        { provide: ModelService, useValue: mockModelService },
        { provide: ToolService, useValue: mockToolService },
        { provide: FileUploadService, useValue: { getReadyFileById: vi.fn() } },
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

  it('should include assistant ID in request', async () => {
    await service.submitChatRequest('Hello', 'session1', undefined, 'assistant1');

    expect(mockChatHttpService.sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        rag_assistant_id: 'assistant1',
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
});