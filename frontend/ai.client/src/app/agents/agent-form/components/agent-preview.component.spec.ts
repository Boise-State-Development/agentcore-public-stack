import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { AgentPreviewComponent } from './agent-preview.component';
import { PreviewChatService } from '../../../assistants/assistant-form/services/preview-chat.service';
import { ModelService } from '../../../session/services/model/model.service';

/**
 * The Designer's preview pane.
 *
 * Inherited from the retired Assistant editor's preview when that page was deleted
 * (Designer Phase 5). The old component had a spec and this one did not, so the live
 * guards below were untested — these port the coverage that still applies and pin the
 * one behavior that differs.
 */
describe('AgentPreviewComponent', () => {
  let component: AgentPreviewComponent;
  let fixture: ComponentFixture<AgentPreviewComponent>;
  let mockPreviewChatService: {
    sendMessage: ReturnType<typeof vi.fn>;
    cancelRequest: ReturnType<typeof vi.fn>;
    clearMessages: ReturnType<typeof vi.fn>;
    reset: ReturnType<typeof vi.fn>;
    messages: ReturnType<typeof signal<never[]>>;
    isLoading: ReturnType<typeof signal<boolean>>;
    streamingMessageId: ReturnType<typeof signal<string | null>>;
    sessionId: ReturnType<typeof signal<string>>;
    hasMessages: ReturnType<typeof signal<boolean>>;
    error: ReturnType<typeof signal<string | null>>;
  };
  let mockModelService: {
    lockToAgentModel: ReturnType<typeof vi.fn>;
    clearAgentModelLock: ReturnType<typeof vi.fn>;
    availableModels: ReturnType<typeof signal<never[]>>;
    agentModelLocked: ReturnType<typeof signal<boolean>>;
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    mockPreviewChatService = {
      sendMessage: vi.fn().mockResolvedValue(undefined),
      cancelRequest: vi.fn(),
      clearMessages: vi.fn(),
      reset: vi.fn(),
      messages: signal([]),
      isLoading: signal(false),
      streamingMessageId: signal(null),
      sessionId: signal('preview-test-session'),
      hasMessages: signal(false),
      error: signal(null),
    };

    // The component pins the chat-input model picker to the agent's model on init and
    // releases it in ngOnDestroy, so both lock methods have to exist or every test dies
    // in cleanup rather than on its assertion.
    mockModelService = {
      lockToAgentModel: vi.fn(),
      clearAgentModelLock: vi.fn(),
      availableModels: signal([]),
      agentModelLocked: signal(false),
    };

    await TestBed.configureTestingModule({
      providers: [{ provide: ModelService, useValue: mockModelService }],
    })
      .overrideComponent(AgentPreviewComponent, {
        set: {
          // Swap the component-level provider so the mock is what gets injected, and
          // strip the template so this does not drag in the whole chat container.
          providers: [{ provide: PreviewChatService, useValue: mockPreviewChatService }],
          template: '<div></div>',
        },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentPreviewComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /**
   * ⚠️ This is the one place the Agent preview deliberately differs from the Assistant
   * preview it replaced, and it looks like a bug if you don't know why.
   *
   * The old editor passed the form's **live, unsaved** instructions as `system_prompt`.
   * An Agent resolves its instructions, model, tools, skills and memory server-side from
   * the saved record (via `rag_assistant_id`), so sending them from here would fight the
   * bindings, and a long persona can blow the `system_prompt` length cap outright (422).
   *
   * The visible trade-off is that the preview reflects what is **saved**, not what is
   * typed. That is the intended behavior — the component does not even accept an
   * `instructions` input. Anyone "fixing" it by threading instructions back through will
   * reintroduce the 422 — hence this test.
   */
  describe('server-side resolution (not live instructions)', () => {
    it('sends no live instructions and opts out of the client-side prompt and tools', () => {
      fixture.componentRef.setInput('agentId', 'ast-001');
      fixture.detectChanges();

      component.onMessageSubmitted({ content: 'hello', timestamp: new Date() });

      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'hello',
        'ast-001',
        undefined,
        undefined,
        { includeSystemPrompt: false, includeEnabledTools: false },
      );
    });

    it('applies the same opts to a starter', () => {
      fixture.componentRef.setInput('agentId', 'ast-001');
      fixture.detectChanges();

      component.onStarterSelected('What is the deadline?');

      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'What is the deadline?',
        'ast-001',
        undefined,
        undefined,
        { includeSystemPrompt: false, includeEnabledTools: false },
      );
    });

    it('forwards file uploads', () => {
      fixture.componentRef.setInput('agentId', 'ast-001');
      fixture.detectChanges();

      component.onMessageSubmitted({
        content: 'see attached',
        timestamp: new Date(),
        fileUploadIds: ['up-1'],
      });

      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'see attached',
        'ast-001',
        undefined,
        ['up-1'],
        { includeSystemPrompt: false, includeEnabledTools: false },
      );
    });
  });

  describe('guards', () => {
    it('does not send while the agent is still a draft with no id', () => {
      fixture.componentRef.setInput('agentId', null);
      fixture.detectChanges();

      component.onMessageSubmitted({ content: 'hello', timestamp: new Date() });
      component.onStarterSelected('hello');

      expect(mockPreviewChatService.sendMessage).not.toHaveBeenCalled();
    });

    it('does not send whitespace-only content', () => {
      fixture.componentRef.setInput('agentId', 'ast-001');
      fixture.detectChanges();

      component.onMessageSubmitted({ content: '   ', timestamp: new Date() });
      component.onStarterSelected('   ');

      expect(mockPreviewChatService.sendMessage).not.toHaveBeenCalled();
    });
  });

  /**
   * The preview's model picker is pinned to the Agent's own model binding. Without the
   * lock it shows the user's global model and lets them switch it, which is a lie — the
   * harness resolves the model from the binding server-side either way.
   *
   * The lock lives in the *root* ModelService, shared with the main chat, which is why
   * releasing it on destroy matters: a leaked lock would follow the user out of the
   * Designer and into an ordinary conversation.
   */
  describe('model lock', () => {
    it('pins the picker to the agent model', () => {
      fixture.componentRef.setInput('modelId', 'claude-opus-5');
      fixture.detectChanges();

      expect(mockModelService.lockToAgentModel).toHaveBeenCalledWith('claude-opus-5');
    });

    it('clears the lock when the agent binds no model', () => {
      fixture.componentRef.setInput('modelId', null);
      fixture.detectChanges();

      expect(mockModelService.clearAgentModelLock).toHaveBeenCalled();
    });

    it('releases the lock on destroy so it does not follow the user into a plain chat', () => {
      fixture.componentRef.setInput('modelId', 'claude-opus-5');
      fixture.detectChanges();
      mockModelService.clearAgentModelLock.mockClear();

      fixture.destroy();

      expect(mockModelService.clearAgentModelLock).toHaveBeenCalled();
    });
  });

  describe('delegation', () => {
    it('delegates clearing to the service', () => {
      component.clearChat();
      expect(mockPreviewChatService.clearMessages).toHaveBeenCalled();
    });

    it('delegates cancellation to the service', () => {
      component.onMessageCancelled();
      expect(mockPreviewChatService.cancelRequest).toHaveBeenCalled();
    });
  });
});
