import { Component, input, output, signal } from '@angular/core';
import { Router, provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentBindingLocksService } from '../../agents/services/agent-binding-locks.service';
import { AgentService } from '../../agents/services/agent.service';
import { UserService } from '../../auth/user.service';
import { ChatInputComponent, ComposerProject } from '../../session/components/chat-input/chat-input.component';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { ConversationBindingService } from '../../session/services/session/conversation-binding.service';
import { SessionPrewarmService } from '../../session/services/session/session-prewarm.service';
import { SessionService } from '../../session/services/session/session.service';
import { VoiceChatService, VoiceStatus } from '../../session/services/voice/voice-chat.service';
import { Project } from '../models/project.model';
import { ProjectComposerComponent } from './project-composer.component';

/** The real composer is the conversation page's; its own spec covers what it does. */
@Component({ selector: 'app-chat-input', template: '' })
class ChatInputStub {
  readonly sessionId = input<string | null>(null);
  readonly project = input<ComposerProject | null>(null);
  readonly allowFolderAttachments = input(false);
  readonly showVoiceControl = input(true);
  readonly voiceAgentId = input<string | null>(null);
  readonly showAnnouncements = input(true);
  readonly autoFocus = input(true);
  readonly fileAttached = output<File>();
  readonly messageSubmitted = output<{
    content: string;
    timestamp: Date;
    fileUploadIds?: string[];
    invokedSkillIds?: string[];
    pastedChars?: number;
  }>();
}

const PROJECT: Project = {
  projectId: 'proj-1',
  name: 'Grant intake',
  description: '',
  ownerEmail: 'owner@example.com',
  ownerName: 'Owner',
  role: 'owner',
  status: 'active',
  editorsManageMembers: false,
  memberCount: 0,
  harnessAgentId: 'agent-harness',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
};

const HARNESS = { agentId: 'agent-harness', bindings: [], modelConfig: { modelId: 'model-x' } };

describe('ProjectComposerComponent', () => {
  let chatRequest: { submitChatRequest: ReturnType<typeof vi.fn> };
  let binding: { knowProject: ReturnType<typeof vi.fn>; queryParamsFor: ReturnType<typeof vi.fn> };
  let voice: { status: ReturnType<typeof signal<VoiceStatus>>; getSessionId: () => string | null };
  let agents: { getAgent: ReturnType<typeof vi.fn> };
  let locks: { apply: ReturnType<typeof vi.fn>; clear: ReturnType<typeof vi.fn>; lockModel: ReturnType<typeof vi.fn> };
  let sessions: { addSessionToCache: ReturnType<typeof vi.fn> };
  let prewarm: { claimNewConversationId: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    chatRequest = { submitChatRequest: vi.fn().mockResolvedValue(undefined) };
    binding = { knowProject: vi.fn(), queryParamsFor: vi.fn().mockReturnValue({ project: 'proj-1', assistantId: null }) };
    voice = { status: signal<VoiceStatus>('idle'), getSessionId: () => 'voice-sess' };
    agents = { getAgent: vi.fn().mockResolvedValue(HARNESS) };
    locks = { apply: vi.fn(), clear: vi.fn(), lockModel: vi.fn() };
    sessions = { addSessionToCache: vi.fn() };
    prewarm = { claimNewConversationId: vi.fn().mockReturnValue(null) };

    TestBed.configureTestingModule({
      providers: [
        { provide: ChatRequestService, useValue: chatRequest },
        { provide: ConversationBindingService, useValue: binding },
        { provide: AgentService, useValue: agents },
        { provide: AgentBindingLocksService, useValue: locks },
        { provide: SessionService, useValue: sessions },
        { provide: SessionPrewarmService, useValue: prewarm },
        { provide: UserService, useValue: { currentUser: () => ({ user_id: 'user-1' }) } },
        { provide: VoiceChatService, useValue: voice },
        provideRouter([]),
      ],
    });
    TestBed.overrideComponent(ProjectComposerComponent, {
      remove: { imports: [ChatInputComponent] },
      add: { imports: [ChatInputStub] },
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(project: Project = PROJECT, voiceAvailable?: boolean) {
    const fixture = TestBed.createComponent(ProjectComposerComponent);
    fixture.componentRef.setInput('project', project);
    if (voiceAvailable !== undefined) fixture.componentRef.setInput('voiceAvailable', voiceAvailable);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const debug = fixture.debugElement.query(By.directive(ChatInputStub));
    return { fixture, input: debug?.componentInstance as ChatInputStub | undefined };
  }

  describe('voice mode (#2021)', () => {
    it('is offered while the project allows it, on the project’s agent', async () => {
      const { input } = await render(PROJECT, true);
      expect(input!.showVoiceControl()).toBe(true);
      expect(input!.voiceAgentId()).toBe('agent-harness');
    });

    it('is not offered when the project keeps it off', async () => {
      expect((await render(PROJECT, false)).input!.showVoiceControl()).toBe(false);
    });

    it('is not offered before the rail knows, or on an archived project', async () => {
      expect((await render(PROJECT)).input!.showVoiceControl()).toBe(false);
    });

    it('opens the new conversation once the session is live', async () => {
      const { fixture } = await render(PROJECT, true);
      const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
      voice.status.set('connecting');
      fixture.detectChanges();
      expect(navigate).not.toHaveBeenCalled();
      voice.status.set('listening');
      fixture.detectChanges();

      expect(binding.knowProject).toHaveBeenCalledWith(PROJECT);
      expect(sessions.addSessionToCache).toHaveBeenCalledWith('voice-sess', 'user-1');
      expect(binding.queryParamsFor).toHaveBeenCalledWith('agent-harness', 'proj-1');
      expect(navigate).toHaveBeenCalledWith(['s', 'voice-sess'], { queryParams: { project: 'proj-1', assistantId: null } });
    });

    it('stays put when the connect fails, and ignores a session already running', async () => {
      voice.status.set('listening'); // running before the page opened
      const { fixture } = await render(PROJECT, true);
      const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
      voice.status.set('speaking');
      fixture.detectChanges();
      voice.status.set('idle');
      fixture.detectChanges();
      voice.status.set('connecting');
      fixture.detectChanges();
      voice.status.set('idle'); // the mic was refused
      fixture.detectChanges();
      expect(navigate).not.toHaveBeenCalled();
    });
  });

  it('uses the conversation composer, naming the project, without voice or announcements', async () => {
    const { input } = await render();
    expect(input).toBeDefined();
    expect(input!.project()).toEqual({ projectId: 'proj-1', name: 'Grant intake' });
    expect(input!.showVoiceControl()).toBe(false);
    expect(input!.showAnnouncements()).toBe(false);
    expect(input!.allowFolderAttachments()).toBe(true);
    expect(input!.sessionId()).toBeNull();
  });

  it('locks the pickers to the harness while it is up, and releases them on destroy', async () => {
    const { fixture } = await render();
    expect(agents.getAgent).toHaveBeenCalledWith('agent-harness');
    expect(locks.apply).toHaveBeenCalledWith(HARNESS, expect.any(Function));
    // The skill resolve's guard holds while this is still the harness being sent to.
    expect(locks.apply.mock.calls[0][1]()).toBe(true);

    locks.clear.mockClear();
    fixture.destroy();
    expect(locks.clear).toHaveBeenCalled();
  });

  it('re-locks the model picker when the rail reports a model change, and not before it knows', async () => {
    const { fixture } = await render();
    expect(locks.lockModel).not.toHaveBeenCalled();

    fixture.componentRef.setInput('projectModelId', 'model-y');
    fixture.detectChanges();
    expect(locks.lockModel).toHaveBeenLastCalledWith('model-y');

    fixture.componentRef.setInput('projectModelId', null);
    fixture.detectChanges();
    expect(locks.lockModel).toHaveBeenLastCalledWith(null);
  });

  it('leaves the pickers free when the harness cannot be read', async () => {
    agents.getAgent.mockRejectedValue(new Error('404'));
    await render();
    expect(locks.apply).not.toHaveBeenCalled();
    expect(locks.clear).toHaveBeenCalled();
  });

  it('offers no composer on an archived project, and locks nothing', async () => {
    const { fixture, input } = await render({ ...PROJECT, status: 'archived' });
    expect(input).toBeUndefined();
    expect(fixture.nativeElement.textContent).toContain('This project is archived.');
    expect(agents.getAgent).not.toHaveBeenCalled();
  });

  it('sends to the harness with everything the composer carried', async () => {
    const { input } = await render();
    input!.messageSubmitted.emit({
      content: 'Summarise the award',
      timestamp: new Date(),
      invokedSkillIds: ['skill-1'],
      pastedChars: 12,
    });
    await Promise.resolve();
    expect(binding.knowProject).toHaveBeenCalledWith(PROJECT);
    expect(chatRequest.submitChatRequest).toHaveBeenCalledWith(
      'Summarise the award',
      null,
      undefined,
      'agent-harness',
      undefined,
      ['skill-1'],
      undefined,
      12,
    );
  });

  it('files attachments under one staged conversation, which the send then starts', async () => {
    const { fixture, input } = await render();
    input!.fileAttached.emit(new File(['a'], 'a.pdf'));
    input!.fileAttached.emit(new File(['b'], 'b.pdf'));
    fixture.detectChanges();

    const staged = input!.sessionId();
    expect(staged).toBeTruthy();
    expect(sessions.addSessionToCache).toHaveBeenCalledTimes(1);
    expect(sessions.addSessionToCache).toHaveBeenCalledWith(staged, 'user-1');

    input!.messageSubmitted.emit({ content: 'Read these', timestamp: new Date(), fileUploadIds: ['u1', 'u2'] });
    await fixture.whenStable();
    expect(chatRequest.submitChatRequest).toHaveBeenCalledWith(
      'Read these',
      staged,
      ['u1', 'u2'],
      'agent-harness',
      undefined,
      undefined,
      undefined,
      undefined,
    );
    fixture.detectChanges();
    expect(input!.sessionId()).toBeNull();
  });

  it('reuses a warmed conversation id for a staged upload', async () => {
    prewarm.claimNewConversationId.mockReturnValue('warm-1');
    const { fixture, input } = await render();
    input!.fileAttached.emit(new File(['a'], 'a.pdf'));
    fixture.detectChanges();
    expect(input!.sessionId()).toBe('warm-1');
  });

  it('says so when the conversation could not be started', async () => {
    chatRequest.submitChatRequest.mockRejectedValue(new Error('boom'));
    const { fixture, input } = await render();
    input!.messageSubmitted.emit({ content: 'Hi', timestamp: new Date() });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]')?.textContent).toContain('could not be started');
  });
});
