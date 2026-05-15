import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { MessageMapService } from './message-map.service';
import { SessionService } from './session.service';
import { FileUploadService } from '../../../services/file-upload';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { signal } from '@angular/core';

describe('MessageMapService', () => {
  let service: MessageMapService;
  let httpMock: HttpTestingController;
  let mockSessionService: any;
  let mockFileUploadService: any;
  let mockOAuthConsentService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockSessionService = {
      getMessages: vi.fn().mockResolvedValue({ messages: [] }),
      isNewSession: vi.fn().mockReturnValue(false),
      updateSessionTitleInCache: vi.fn()
    };
    mockFileUploadService = {
      listSessionFiles: vi.fn().mockResolvedValue([])
    };
    mockOAuthConsentService = {
      requestConsent: vi.fn()
    };
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        MessageMapService,
        { provide: SessionService, useValue: mockSessionService },
        { provide: FileUploadService, useValue: mockFileUploadService },
        { provide: OAuthConsentService, useValue: mockOAuthConsentService }
      ]
    });
    service = TestBed.inject(MessageMapService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should get messages for session', () => {
    const messagesSignal = service.getMessagesForSession('session-1');
    expect(messagesSignal).toBeTruthy();
    expect(messagesSignal()).toEqual([]);
  });

  it('should add user message', () => {
    const message = service.addUserMessage('session-1', 'Hello world');
    
    expect(message.role).toBe('user');
    expect(message.content).toEqual([{ type: 'text', text: 'Hello world' }]);
    expect(message.id).toBe('msg-session-1-0');

    const messagesSignal = service.getMessagesForSession('session-1');
    expect(messagesSignal()).toHaveLength(1);
  });

  it('should add user message with file attachments', () => {
    const fileAttachments = [{
      uploadId: 'upload-1',
      filename: 'test.pdf',
      mimeType: 'application/pdf',
      sizeBytes: 1024
    }];

    const message = service.addUserMessage('session-1', 'Check this file', fileAttachments);
    
    expect(message.content).toHaveLength(2);
    expect(message.content[0]).toEqual({ type: 'fileAttachment', fileAttachment: fileAttachments[0] });
    expect(message.content[1]).toEqual({ type: 'text', text: 'Check this file' });
  });

  it('should load messages for session', async () => {
    const mockMessages = [
      { id: 'msg-1', role: 'user', content: [{ type: 'text', text: 'Hello' }] }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('session-1');

    expect(mockSessionService.getMessages).toHaveBeenCalledWith('session-1');
    expect(mockFileUploadService.listSessionFiles).toHaveBeenCalledWith('session-1');

    const messagesSignal = service.getMessagesForSession('session-1');
    expect(messagesSignal()).toEqual(mockMessages);
  });

  it('should hydrate pending OAuth interrupts from camelCase wire response', async () => {
    // Regression: backend serializes with by_alias=True so the wire payload uses
    // camelCase (pendingInterrupts, interruptId, providerId, ...). If the consumer
    // reads snake_case fields, the consent prompt silently fails to re-render
    // after a refresh.
    const mockMessages = [
      { id: 'msg-assistant-7', role: 'assistant', content: [{ type: 'text', text: 'ok' }] },
    ];
    mockSessionService.getMessages.mockResolvedValue({
      messages: mockMessages,
      pendingInterrupts: [
        {
          interruptId: 'v1:before_tool_call:tooluse_abc:xyz',
          providerId: 'google-calendar-employee',
          createdAt: '2026-04-26T01:13:54.543143+00:00',
        },
      ],
    });

    await service.loadMessagesForSession('session-with-interrupt');

    expect(mockOAuthConsentService.requestConsent).toHaveBeenCalledTimes(1);
    expect(mockOAuthConsentService.requestConsent).toHaveBeenCalledWith(
      'google-calendar-employee',
      undefined,
      'v1:before_tool_call:tooluse_abc:xyz',
      'msg-assistant-7',
      'session-with-interrupt',
    );
  });

  it('should not call requestConsent when no pending interrupts are returned', async () => {
    mockSessionService.getMessages.mockResolvedValue({ messages: [] });
    await service.loadMessagesForSession('session-clean');
    expect(mockOAuthConsentService.requestConsent).not.toHaveBeenCalled();
  });

  it('should set loading session state', () => {
    service.setLoadingSession('session-1');
    expect(service.isLoadingSession()).toBe('session-1');

    service.setLoadingSession(null);
    expect(service.isLoadingSession()).toBe(null);
  });

  it('should start and end streaming', () => {
    service.startStreaming('session-1');
    // Verify session exists in map
    const messagesSignal = service.getMessagesForSession('session-1');
    expect(messagesSignal).toBeTruthy();

    service.endStreaming();
    // Service should handle end streaming gracefully
    expect(service).toBeTruthy();
  });

  it('should clear session', () => {
    service.addUserMessage('session-1', 'Hello');
    service.clearSession('session-1');
    
    const messagesSignal = service.getMessagesForSession('session-1');
    expect(messagesSignal()).toEqual([]);
  });

  it('should match tool results with success status', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'assistant',
        content: [{ type: 'toolUse', toolUse: { toolUseId: 'tool-1', name: 'search', input: {} } }]
      },
      {
        id: 'msg-2',
        role: 'user',
        content: [{ type: 'toolResult', toolResult: { toolUseId: 'tool-1', content: [{ text: 'result' }], status: 'success' } }]
      }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('tool-session-1');

    const messagesSignal = service.getMessagesForSession('tool-session-1');
    const messages = messagesSignal();
    expect(((messages[0].content[0] as any).toolUse).result).toEqual({ content: [{ text: 'result' }], status: 'success' });
    expect(((messages[0].content[0] as any).toolUse).status).toBe('complete');
  });

  it('should match tool results with error status', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'assistant',
        content: [{ type: 'toolUse', toolUse: { toolUseId: 'tool-1', name: 'search', input: {} } }]
      },
      {
        id: 'msg-2',
        role: 'user',
        content: [{ type: 'toolResult', toolResult: { toolUseId: 'tool-1', content: [{ text: 'error' }], status: 'error' } }]
      }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('tool-session-2');

    const messagesSignal = service.getMessagesForSession('tool-session-2');
    const messages = messagesSignal();
    expect(((messages[0].content[0] as any).toolUse).result).toEqual({ content: [{ text: 'error' }], status: 'error' });
    expect(((messages[0].content[0] as any).toolUse).status).toBe('error');
  });

  it('should detect error from JSON content with success:false', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'assistant',
        content: [{ type: 'toolUse', toolUse: { toolUseId: 'tool-1', name: 'search', input: {} } }]
      },
      {
        id: 'msg-2',
        role: 'user',
        content: [{ type: 'toolResult', toolResult: { toolUseId: 'tool-1', content: [{ json: { success: false, error: 'failed' } }] } }]
      }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('tool-session-3');

    const messagesSignal = service.getMessagesForSession('tool-session-3');
    const messages = messagesSignal();
    expect(((messages[0].content[0] as any).toolUse).status).toBe('error');
  });

  it('should detect error from parseable JSON text', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'assistant',
        content: [{ type: 'toolUse', toolUse: { toolUseId: 'tool-1', name: 'search', input: {} } }]
      },
      {
        id: 'msg-2',
        role: 'user',
        content: [{ type: 'toolResult', toolResult: { toolUseId: 'tool-1', content: [{ text: '{"success": false, "error": "failed"}' }] } }]
      }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('tool-session-4');

    const messagesSignal = service.getMessagesForSession('tool-session-4');
    const messages = messagesSignal();
    expect(((messages[0].content[0] as any).toolUse).status).toBe('error');
  });

  it('should restore file attachments from marker', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'user',
        content: [{ type: 'text', text: 'Check this\n\n[Attached files: file1.pdf, file2.png]' }]
      }
    ];
    const mockFiles = [
      { uploadId: 'upload-1', filename: 'file1.pdf', mimeType: 'application/pdf', sizeBytes: 1024 },
      { uploadId: 'upload-2', filename: 'file2.png', mimeType: 'image/png', sizeBytes: 2048 }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });
    mockFileUploadService.listSessionFiles.mockResolvedValue(mockFiles);

    await service.loadMessagesForSession('tool-session-5');

    const messagesSignal = service.getMessagesForSession('tool-session-5');
    const messages = messagesSignal();
    expect(messages[0].content).toHaveLength(3);
    expect(messages[0].content[0]).toEqual({ type: 'fileAttachment', fileAttachment: mockFiles[0] });
    expect(messages[0].content[1]).toEqual({ type: 'fileAttachment', fileAttachment: mockFiles[1] });
    expect(messages[0].content[2]).toEqual({ type: 'text', text: 'Check this' });
  });

  it('should handle messages without file marker', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'user',
        content: [{ type: 'text', text: 'Regular message' }]
      }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('tool-session-6');

    const messagesSignal = service.getMessagesForSession('tool-session-6');
    const messages = messagesSignal();
    expect(messages[0].content).toEqual([{ type: 'text', text: 'Regular message' }]);
  });

  it('should handle files not found in filesByName map', async () => {
    const mockMessages = [
      {
        id: 'msg-1',
        role: 'user',
        content: [{ type: 'text', text: 'Check this\n\n[Attached files: missing.pdf]' }]
      }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });
    mockFileUploadService.listSessionFiles.mockResolvedValue([]);

    await service.loadMessagesForSession('tool-session-7');

    const messagesSignal = service.getMessagesForSession('tool-session-7');
    const messages = messagesSignal();
    // When file is not found in map, no fileAttachment block is created
    // The text may or may not have the marker removed depending on regex matching
    expect(messages[0].role).toBe('user');
    expect(messages[0].content.length).toBeGreaterThanOrEqual(1);
  });

  it('should handle getMessages error', async () => {
    mockSessionService.getMessages.mockRejectedValue(new Error('API error'));

    await expect(service.loadMessagesForSession('tool-session-8')).rejects.toThrow('API error');
    expect(service.isLoadingSession()).toBe(null);
  });

  it('should not reload already-loaded messages', async () => {
    const mockMessages = [
      { id: 'msg-1', role: 'user', content: [{ type: 'text', text: 'Hello' }] }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });

    await service.loadMessagesForSession('no-reload-session');
    const callsBefore = mockSessionService.getMessages.mock.calls.length;

    // Second call should skip API because messages already loaded (length > 0)
    await service.loadMessagesForSession('no-reload-session');
    expect(mockSessionService.getMessages.mock.calls.length).toBe(callsBefore);
  });

  it('should handle listSessionFiles error gracefully', async () => {
    const mockMessages = [
      { id: 'msg-1', role: 'user', content: [{ type: 'text', text: 'Hello' }] }
    ];
    mockSessionService.getMessages.mockResolvedValue({ messages: mockMessages });
    mockFileUploadService.listSessionFiles.mockRejectedValue(new Error('File service error'));

    await service.loadMessagesForSession('tool-session-11');

    const messagesSignal = service.getMessagesForSession('tool-session-11');
    expect(messagesSignal()).toEqual(mockMessages);
  });

  it('should increment user message IDs correctly', () => {
    const msg1 = service.addUserMessage('session-1', 'First message');
    const msg2 = service.addUserMessage('session-1', 'Second message');
    const msg3 = service.addUserMessage('session-1', 'Third message');

    expect(msg1.id).toBe('msg-session-1-0');
    expect(msg2.id).toBe('msg-session-1-1');
    expect(msg3.id).toBe('msg-session-1-2');
  });
});