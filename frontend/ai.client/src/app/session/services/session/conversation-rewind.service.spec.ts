import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationRewindService } from './conversation-rewind.service';
import { SessionService } from './session.service';
import { MessageMapService } from './message-map.service';
import { ComposerDraftService } from './composer-draft.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ToastService } from '../../../services/toast/toast.service';
import { FEATURES } from '../../../services/features';
import { environment } from '../../../../environments/environment';
import type { Message } from '../models/message.model';

describe('ConversationRewindService', () => {
  let service: ConversationRewindService;
  let sessionService: { rewindSession: ReturnType<typeof vi.fn> };
  let messageMap: { rewindToMessage: ReturnType<typeof vi.fn> };
  let chatState: {
    isSessionLoading: ReturnType<typeof vi.fn>;
    setLastTurnContinuable: ReturnType<typeof vi.fn>;
    setLastTurnInterrupted: ReturnType<typeof vi.fn>;
  };
  let toast: { error: ReturnType<typeof vi.fn> };
  let composerDraft: ComposerDraftService;

  const userMessage = (overrides: Partial<Message> = {}): Message => ({
    id: 'msg-sess-1-4',
    role: 'user',
    content: [{ type: 'text', text: 'what changed?' }],
    ...overrides,
  });

  function setup(enabled = true): void {
    sessionService = { rewindSession: vi.fn() };
    messageMap = { rewindToMessage: vi.fn().mockReturnValue(true) };
    chatState = {
      isSessionLoading: vi.fn().mockReturnValue(false),
      setLastTurnContinuable: vi.fn(),
      setLastTurnInterrupted: vi.fn(),
    };
    toast = { error: vi.fn() };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: FEATURES, useValue: { ...environment.features, sessionRewind: enabled } },
        { provide: SessionService, useValue: sessionService },
        { provide: MessageMapService, useValue: messageMap },
        { provide: ChatStateService, useValue: chatState },
        { provide: ToastService, useValue: toast },
      ],
    });
    service = TestBed.inject(ConversationRewindService);
    composerDraft = TestBed.inject(ComposerDraftService);
  }

  beforeEach(() => setup());

  it('rewinds, trims the thread to the server count and prefills the composer', async () => {
    sessionService.rewindSession.mockResolvedValue({
      nextMessageIndex: 8,
      prefillText: 'what changed?',
      rewoundMessageIds: ['msg-sess-1-4', 'msg-sess-1-5'],
      epoch: 1,
    });

    await expect(service.rewind(userMessage())).resolves.toBe(true);

    expect(sessionService.rewindSession).toHaveBeenCalledWith('sess-1', 'msg-sess-1-4');
    expect(messageMap.rewindToMessage).toHaveBeenCalledWith('sess-1', 'msg-sess-1-4', 8);
    expect(chatState.setLastTurnContinuable).toHaveBeenCalledWith('sess-1', false);
    expect(composerDraft.consume('sess-1')?.text).toBe('what changed?');
    expect(service.pendingMessageId()).toBeNull();
  });

  it('is unavailable while that conversation streams', async () => {
    chatState.isSessionLoading.mockReturnValue(true);

    expect(service.canRewind(userMessage())).toBe(false);
    await expect(service.rewind(userMessage())).resolves.toBe(false);
    expect(sessionService.rewindSession).not.toHaveBeenCalled();
  });

  it('never offers itself on assistant messages, steers or unsent messages', () => {
    expect(service.canRewind(userMessage({ role: 'assistant' }))).toBe(false);
    expect(service.canRewind(userMessage({ steering: true }))).toBe(false);
    expect(service.canRewind(userMessage({ sendFailure: { reason: 'x' } }))).toBe(false);
    expect(service.canRewind(userMessage({ id: 'local-placeholder' }))).toBe(false);
  });

  it('is off with the feature flag', () => {
    setup(false);
    expect(service.enabled).toBe(false);
    expect(service.canRewind(userMessage())).toBe(false);
  });

  it("shows the server's reason and leaves the thread alone when refused", async () => {
    sessionService.rewindSession.mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { detail: "That message is part of the conversation's summary and can't be rewound to" },
      }),
    );

    await expect(service.rewind(userMessage())).resolves.toBe(false);

    expect(toast.error).toHaveBeenCalledWith(
      "Couldn't rewind",
      "That message is part of the conversation's summary and can't be rewound to",
    );
    expect(messageMap.rewindToMessage).not.toHaveBeenCalled();
    expect(composerDraft.pending()).toBeNull();
  });
});
