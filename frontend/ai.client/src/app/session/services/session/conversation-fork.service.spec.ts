import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Router } from '@angular/router';
import { signal } from '@angular/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationForkService } from './conversation-fork.service';
import { SessionService } from './session.service';
import { ComposerDraftStorageService } from './composer-draft-storage.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ToastService } from '../../../services/toast/toast.service';
import { UserService } from '../../../auth/user.service';
import { FEATURES } from '../../../services/features';
import { environment } from '../../../../environments/environment';
import type { Message } from '../models/message.model';

describe('ConversationForkService', () => {
  let service: ConversationForkService;
  let sessionService: { forkSession: ReturnType<typeof vi.fn>; addSessionToCache: ReturnType<typeof vi.fn> };
  let drafts: { write: ReturnType<typeof vi.fn> };
  let router: { navigate: ReturnType<typeof vi.fn> };
  let chatState: { isSessionLoading: ReturnType<typeof vi.fn> };
  let toast: { error: ReturnType<typeof vi.fn> };

  const userMessage = (overrides: Partial<Message> = {}): Message => ({
    id: 'msg-parent-1-4',
    role: 'user',
    content: [{ type: 'text', text: 'try it the other way' }],
    ...overrides,
  });

  function setup(enabled = true): void {
    sessionService = { forkSession: vi.fn(), addSessionToCache: vi.fn() };
    drafts = { write: vi.fn() };
    router = { navigate: vi.fn().mockResolvedValue(true) };
    chatState = { isSessionLoading: vi.fn().mockReturnValue(false) };
    toast = { error: vi.fn() };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: FEATURES, useValue: { ...environment.features, sessionFork: enabled } },
        { provide: SessionService, useValue: sessionService },
        { provide: ComposerDraftStorageService, useValue: drafts },
        { provide: Router, useValue: router },
        { provide: ChatStateService, useValue: chatState },
        { provide: ToastService, useValue: toast },
        { provide: UserService, useValue: { currentUser: signal({ user_id: 'u1' }) } },
      ],
    });
    service = TestBed.inject(ConversationForkService);
  }

  beforeEach(() => setup());

  it('parks the text as the fork\'s draft before opening it', async () => {
    sessionService.forkSession.mockResolvedValue({
      sessionId: 'child-1',
      title: 'Plan (fork)',
      prefillText: 'try it the other way',
      rootSessionId: 'parent-1',
    });

    await expect(service.fork(userMessage())).resolves.toBe('child-1');

    expect(sessionService.forkSession).toHaveBeenCalledWith('parent-1', 'msg-parent-1-4');
    expect(drafts.write).toHaveBeenCalledWith('child-1', { text: 'try it the other way' });
    expect(sessionService.addSessionToCache).toHaveBeenCalledWith('child-1', 'u1', 'Plan (fork)');
    expect(router.navigate).toHaveBeenCalledWith(['/s', 'child-1']);
    // Written before navigating, so the composer adopts it on arrival.
    expect(drafts.write.mock.invocationCallOrder[0]).toBeLessThan(router.navigate.mock.invocationCallOrder[0]);
  });

  it('stays put and explains a refusal', async () => {
    sessionService.forkSession.mockRejectedValue(
      new HttpErrorResponse({ status: 409, error: { detail: 'That message has been rewound away' } }),
    );

    await expect(service.fork(userMessage())).resolves.toBeNull();

    expect(toast.error).toHaveBeenCalledWith("Couldn't fork", 'That message has been rewound away');
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('is unavailable while streaming, off the flag, and on anything but a numbered user message', () => {
    chatState.isSessionLoading.mockReturnValue(true);
    expect(service.canFork(userMessage())).toBe(false);
    chatState.isSessionLoading.mockReturnValue(false);
    expect(service.canFork(userMessage({ role: 'assistant' }))).toBe(false);
    expect(service.canFork(userMessage({ steering: true }))).toBe(false);
    expect(service.canFork(userMessage({ id: 'local' }))).toBe(false);
    setup(false);
    expect(service.canFork(userMessage())).toBe(false);
  });
});
