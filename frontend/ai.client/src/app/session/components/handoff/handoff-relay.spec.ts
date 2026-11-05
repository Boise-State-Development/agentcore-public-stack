import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Router, provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { environment } from '../../../../environments/environment';
import { FEATURES } from '../../../services/features';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { UserQuestionService } from '../../../services/user-question/user-question.service';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { ChatHttpService } from '../../services/chat/chat-http.service';
import { ChatRequestService } from '../../services/chat/chat-request.service';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { StreamParserService } from '../../services/chat/stream-parser.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import type { HandoffView, RelayedPrompt } from '../../services/handoff/handoff.models';
import type { Message } from '../../services/models/message.model';
import { MessageListComponent } from '../message-list/message-list.component';

/**
 * Relayed prompts (handoffs spec §4.3, PR-0.3c), through the real message list, the real interrupt
 * services and the real resume path. Only the transport is a fake: `sendChatRequest` records the
 * request and, where a test needs the server's answer, feeds the real stream parser the frame the
 * Runtime streams.
 */
describe('Relayed prompts', () => {
  let fixture: ComponentFixture<MessageListComponent>;
  let send: ReturnType<typeof vi.fn>;
  let navigate: ReturnType<typeof vi.spyOn>;

  const APPROVAL: RelayedPrompt = {
    interruptId: 'a1',
    kind: 'tool_approval',
    toolUseId: 'tu1',
    toolName: 'create_assignment',
    toolInput: JSON.stringify({ course: 101 }),
    message: 'Creates an assignment in Canvas',
  };
  const QUESTION: RelayedPrompt = {
    interruptId: 'q1',
    kind: 'user_question',
    questions: JSON.stringify([
      { header: 'Scale', question: 'Which scale?', multiSelect: false, options: [{ label: '4-point' }, { label: '5-point' }] },
    ]),
  };
  const CONSENT: RelayedPrompt = { interruptId: 'i1', kind: 'oauth', providerId: 'canvas' };
  const SIGN_IN: RelayedPrompt = { interruptId: 'b1', kind: 'browser_login' };

  function message(index: number): Message {
    return {
      id: `msg-src-${index}`,
      role: 'user',
      content: [{ type: 'text', text: `message ${index}` }],
      createdAt: '2026-01-01T00:00:00Z',
    } as unknown as Message;
  }

  function waiting(prompts: RelayedPrompt[], overrides: Partial<HandoffView> = {}): HandoffView {
    return {
      handoffId: 'h1',
      sessionId: 'src',
      kind: 'mention',
      status: 'needs_input',
      agentId: 'rubric',
      agentName: 'Rubric Builder',
      request: 'draft a rubric for this',
      sourceMessageIndex: 1,
      excerptTokens: 40,
      createdAt: '2026-01-01T00:00:00Z',
      updatedAt: '2026-01-01T00:00:00Z',
      targetSessionId: 'tgt',
      waitingOn: { kind: 'oauth_consent', name: 'canvas' },
      relayedPrompts: prompts,
      ...overrides,
    };
  }

  function render(): void {
    TestBed.inject(ChatStateService).setViewedSession('src');
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', [message(0), message(1)]);
    fixture.componentRef.setInput('sessionId', 'src');
    fixture.detectChanges();
  }

  function refresh(): void {
    TestBed.tick();
    fixture.detectChanges();
  }

  function relay(): HTMLElement | null {
    return fixture.nativeElement.querySelector('app-handoff-row [data-testid="handoff-relay"]');
  }

  function notice(): string {
    return fixture.nativeElement.querySelector('[data-testid="handoff-relay-notice"]')?.textContent?.trim() ?? '';
  }

  function button(label: string): HTMLButtonElement {
    const found = relay()?.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
    if (!found) throw new Error(`No "${label}" button in the relay`);
    return found;
  }

  /** The source thread's own prompt slots, which must never hold a target's prompt. */
  function sourcePromptSlots() {
    const list = fixture.componentInstance as unknown as {
      unanchoredInterrupts: () => unknown[];
      pendingToolApprovals: () => unknown[];
      pendingUserQuestions: () => unknown[];
      pendingBrowserLogins: () => unknown[];
    };
    return [
      ...list.unanchoredInterrupts(),
      ...list.pendingToolApprovals(),
      ...list.pendingUserQuestions(),
      ...list.pendingBrowserLogins(),
    ];
  }

  function resumeRequest(): { body: Record<string, unknown>; options: unknown } {
    const [body, options] = send.mock.calls[send.mock.calls.length - 1];
    return { body, options };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    send = vi.fn().mockResolvedValue(undefined);
    TestBed.configureTestingModule({
      imports: [MessageListComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideMarkdown(),
        { provide: FEATURES, useValue: { ...environment.features, handoffs: true } },
        { provide: ChatHttpService, useValue: { sendChatRequest: send } },
      ],
    });
    // The resume handlers are the chat layer's; constructing it registers them, as the app does.
    TestBed.inject(ChatRequestService);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });

  afterEach(() => {
    fixture?.destroy();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe('one per kind', () => {
    it('OAuth consent: Connect resumes the target with its interrupt id and leaves the user in the source', async () => {
      vi.spyOn(TestBed.inject(UserConnectorsService), 'initiateConsent').mockResolvedValue({
        connected: false,
        authorizationUrl: 'https://auth.example/consent',
      } as never);
      vi.spyOn(window, 'open').mockReturnValue({ closed: false } as Window);
      TestBed.inject(HandoffStateService).upsertRow(waiting([CONSENT]));
      render();
      refresh();

      expect(relay()?.textContent).toContain('Rubric Builder needs you to connect Canvas to keep going.');
      button('Connect to Canvas').click();
      await vi.waitFor(() => expect(window.open).toHaveBeenCalled());
      window.dispatchEvent(
        new MessageEvent('message', {
          origin: window.location.origin,
          data: { type: 'agentcore-oauth-complete', status: 'success', providerId: 'canvas', error: null },
        }),
      );

      await vi.waitFor(() => expect(send).toHaveBeenCalled());
      const { body, options } = resumeRequest();
      expect(body).toMatchObject({
        session_id: 'tgt',
        message: '',
        interrupt_responses: [{ interruptId: 'i1', response: 'consented' }],
      });
      expect(options).toEqual({ quietConflict: true });
      expect(TestBed.inject(ChatStateService).viewedSessionId()).toBe('src');
      expect(navigate).not.toHaveBeenCalled();
    });

    it('tool approval: Approve and Deny answer the target', async () => {
      TestBed.inject(HandoffStateService).upsertRow(waiting([APPROVAL]));
      render();
      refresh();

      expect(relay()?.textContent).toContain('Rubric Builder needs your approval to keep going.');
      expect(relay()?.textContent).toContain('create_assignment');
      expect(relay()?.querySelector('button[aria-label="Decline tool call"]')).not.toBeNull();
      button('Approve tool call').click();

      await vi.waitFor(() => expect(send).toHaveBeenCalled());
      expect(resumeRequest().body).toMatchObject({
        session_id: 'tgt',
        interrupt_responses: [{ interruptId: 'a1', response: 'approved' }],
      });
      expect(TestBed.inject(ChatStateService).viewedSessionId()).toBe('src');
      expect(navigate).not.toHaveBeenCalled();
    });

    it('ask_user_question: the same picker, plus Open conversation', () => {
      TestBed.inject(HandoffStateService).upsertRow(waiting([QUESTION], { waitingOn: { kind: 'user_question', name: 'Scale' } }));
      render();
      refresh();

      expect(relay()?.textContent).toContain('Rubric Builder has a question for you.');
      expect(relay()?.querySelector('app-user-question-prompt')).not.toBeNull();
      expect(relay()?.textContent).toContain('Which scale?');
      const open = relay()?.querySelector<HTMLAnchorElement>('a');
      expect(open?.textContent?.trim()).toBe('Open conversation');
      expect(open?.getAttribute('href')).toContain('/s/tgt');
    });

    it('browser sign-in: Open conversation only', () => {
      TestBed.inject(HandoffStateService).upsertRow(waiting([SIGN_IN], { waitingOn: { kind: 'browser_login', name: '' } }));
      render();
      refresh();

      expect(relay()?.textContent).toContain('Rubric Builder needs you to sign in to keep going.');
      expect(relay()?.querySelectorAll('button').length).toBe(0);
      expect(relay()?.querySelector('a')?.getAttribute('href')).toContain('/s/tgt');
    });

    it('a pre-flight consent (no interrupt id) is not relayed', () => {
      // Nothing is paused, so the target wrote no breadcrumb and the row relays nothing.
      TestBed.inject(OAuthConsentService).requestConsent('canvas', 'https://auth.example/c', undefined, undefined, 'tgt');
      TestBed.inject(HandoffStateService).upsertRow(waiting([]));
      render();
      refresh();

      expect(relay()).toBeNull();
      expect(sourcePromptSlots()).toEqual([]);
    });

    it('several prompts from one pause relay as one block, one consent per provider', () => {
      TestBed.inject(HandoffStateService).upsertRow(
        waiting([CONSENT, { ...CONSENT, interruptId: 'i2' }, { interruptId: 'i3', kind: 'oauth', providerId: 'github' }, APPROVAL]),
      );
      render();
      refresh();

      expect(fixture.nativeElement.querySelectorAll('[data-testid="handoff-relay"]').length).toBe(1);
      expect(relay()?.textContent).toContain('Rubric Builder needs your input to keep going.');
      expect(relay()?.querySelectorAll('app-oauth-consent-prompt').length).toBe(2);
      expect(relay()?.querySelectorAll('app-tool-approval-prompt').length).toBe(1);
    });
  });

  it("never renders a target's prompt in the source's own prompt slot", () => {
    TestBed.inject(HandoffStateService).upsertRow(waiting([CONSENT, APPROVAL, QUESTION, SIGN_IN]));
    render();
    refresh();

    // Registered in the shared interrupt services, tagged with the target's session…
    expect(TestBed.inject(ToolApprovalService).pending().map((r) => r.sessionId)).toEqual(['tgt']);
    expect(TestBed.inject(UserQuestionService).pending().map((r) => r.sessionId)).toEqual(['tgt']);
    expect(TestBed.inject(OAuthConsentService).pending().map((r) => r.sessionId)).toEqual(['tgt']);
    // …so the source's slots stay empty and every prompt is inside the handoff row.
    expect(sourcePromptSlots()).toEqual([]);
    const outside = Array.from(
      fixture.nativeElement.querySelectorAll('app-oauth-consent-prompt, app-tool-approval-prompt, app-user-question-prompt'),
    ).filter((el) => !(el as HTMLElement).closest('[data-testid="handoff-relay"]'));
    expect(outside).toEqual([]);
  });

  it('waits for a running source turn to end; the amber mark does not', () => {
    const chatState = TestBed.inject(ChatStateService);
    TestBed.inject(HandoffStateService).upsertRow(waiting([CONSENT]));
    chatState.setChatLoading('src', true);
    render();
    refresh();

    expect(relay()).toBeNull();
    const row = fixture.nativeElement.querySelector('app-handoff-row a') as HTMLElement;
    expect(row.textContent).toContain('Waiting for you to connect Canvas');

    chatState.setChatLoading('src', false);
    refresh();

    expect(relay()).not.toBeNull();
  });

  it('first answer wins: the resume guard refusing an answered pause reads "Already answered" and refreshes', async () => {
    const parser = TestBed.inject(StreamParserService);
    const http = TestBed.inject(HttpTestingController);
    // The other surface answered first, so the snapshot is gone and the Runtime's guard streams
    // its refusal (`_resume_unavailable_turn`): a conversational error, never an HTTP one.
    send.mockImplementation(async (request: { session_id: string }) => {
      parser.parseEventSourceMessage(request.session_id, 'stream_error', {
        type: 'stream_error',
        code: 'bad_request',
        message: 'The request that was waiting for your answer can no longer pick up where it left off.',
        recoverable: false,
        metadata: { error_kind: 'resume_unavailable', reason: 'missing' },
      });
    });
    TestBed.inject(HandoffStateService).upsertRow(waiting([APPROVAL]));
    render();
    refresh();

    button('Approve tool call').click();
    await vi.waitFor(() => {
      refresh();
      expect(notice()).toBe('Already answered');
    });

    // The refresh: the target's thread is re-read (dropping the unpersisted refusal) and so is the row.
    // `match` takes what it finds, so collect the row re-read as it arrives.
    const reread: ReturnType<HttpTestingController['match']> = [];
    await vi.waitFor(() => {
      reread.push(...http.match((r) => r.url.endsWith('/sessions/src/handoffs/h1')));
      expect(reread.length).toBeGreaterThan(0);
    });
    reread.forEach((req) => req.flush(waiting([], { status: 'running', relayedPrompts: undefined })));
    const counted = http
      .match((r) => r.url.endsWith('/notifications/events'))
      .map((req) => req.request.body);
    expect(counted).toContainEqual({ metric: 'RelayedPromptConflict', interruptKind: 'tool_approval' });
    expect(fixture.nativeElement.textContent).not.toContain('can no longer pick up');
  });

  it('replays after reload, and goes away when the pause is superseded', () => {
    const state = TestBed.inject(HandoffStateService);
    render();
    // What `GET /messages` hands MessageMapService on a reload.
    state.hydrate('src', [waiting([APPROVAL])], null);
    refresh();

    expect(relay()?.querySelector('app-tool-approval-prompt')).not.toBeNull();

    // A fresh turn in the target superseded the pause: the next read has no relay.
    state.hydrate('src', [waiting([], { status: 'running', relayedPrompts: undefined, waitingOn: undefined })], null);
    refresh();

    expect(relay()).toBeNull();
    expect(TestBed.inject(ToolApprovalService).pending()).toEqual([]);
  });

  it('counts a relayed prompt once when the user can see it', () => {
    const http = TestBed.inject(HttpTestingController);
    TestBed.inject(HandoffStateService).upsertRow(waiting([APPROVAL]));
    render();
    refresh();
    refresh();

    const shown = http
      .match((r) => r.url.endsWith('/sessions/src/handoffs/h1/notifications/events'))
      .map((req) => req.request.body);
    expect(shown).toEqual([{ metric: 'RelayedPromptShown', interruptKind: 'tool_approval' }]);
  });

  it('puts a consent back after a route change clears the consent queue', () => {
    TestBed.inject(HandoffStateService).upsertRow(waiting([CONSENT]));
    render();
    refresh();
    expect(relay()?.querySelector('app-oauth-consent-prompt')).not.toBeNull();

    TestBed.inject(OAuthConsentService).clear();
    refresh();

    expect(relay()?.querySelector('app-oauth-consent-prompt')).not.toBeNull();
  });
});
