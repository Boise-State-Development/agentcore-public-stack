import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { AssistantPreviewComponent } from './assistant-preview.component';
import { PreviewChatService } from '../services/preview-chat.service';

describe('AssistantPreviewComponent', () => {
  let component: AssistantPreviewComponent;
  let fixture: ComponentFixture<AssistantPreviewComponent>;
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

    await TestBed.configureTestingModule({})
      .overrideComponent(AssistantPreviewComponent, {
        set: {
          // Replace the component-level providers so our mock gets injected
          providers: [{ provide: PreviewChatService, useValue: mockPreviewChatService }],
          // Use a minimal template to avoid pulling in child components
          template: '<div></div>',
        },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AssistantPreviewComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('passing live instructions to sendMessage', () => {
    it('should pass current instructions when submitting a message', () => {
      fixture.componentRef.setInput('assistantId', 'ast-123');
      fixture.componentRef.setInput('name', 'Test Bot');
      fixture.componentRef.setInput('instructions', 'You are a pirate. Speak like a pirate.');
      fixture.detectChanges();

      component.onMessageSubmitted({
        content: 'Hello',
        timestamp: new Date(),
      });

      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'Hello',
        'ast-123',
        'You are a pirate. Speak like a pirate.',
        undefined,
      );
    });

    it('should pass updated instructions after form change', () => {
      fixture.componentRef.setInput('assistantId', 'ast-123');
      fixture.componentRef.setInput('name', 'Test Bot');
      fixture.componentRef.setInput('instructions', 'Version 1 instructions');
      fixture.detectChanges();

      component.onMessageSubmitted({ content: 'Hi', timestamp: new Date() });
      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'Hi',
        'ast-123',
        'Version 1 instructions',
        undefined,
      );

      // Simulate parent updating the input (user edited the form)
      fixture.componentRef.setInput('instructions', 'Version 2 instructions');
      fixture.detectChanges();

      component.onMessageSubmitted({ content: 'Hi again', timestamp: new Date() });
      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'Hi again',
        'ast-123',
        'Version 2 instructions',
        undefined,
      );
    });

    it('should pass instructions when a starter is selected', () => {
      fixture.componentRef.setInput('assistantId', 'ast-456');
      fixture.componentRef.setInput('name', 'Starter Bot');
      fixture.componentRef.setInput('instructions', 'Be concise and helpful.');
      fixture.detectChanges();

      component.onStarterSelected('Tell me a joke');

      expect(mockPreviewChatService.sendMessage).toHaveBeenCalledWith(
        'Tell me a joke',
        'ast-456',
        'Be concise and helpful.',
      );
    });

    it('should not send message when assistantId is null', () => {
      fixture.componentRef.setInput('assistantId', null);
      fixture.componentRef.setInput('instructions', 'Some instructions');
      fixture.detectChanges();

      component.onMessageSubmitted({ content: 'Hello', timestamp: new Date() });

      expect(mockPreviewChatService.sendMessage).not.toHaveBeenCalled();
    });

    it('should not send message when content is empty', () => {
      fixture.componentRef.setInput('assistantId', 'ast-123');
      fixture.componentRef.setInput('instructions', 'Some instructions');
      fixture.detectChanges();

      component.onMessageSubmitted({ content: '   ', timestamp: new Date() });

      expect(mockPreviewChatService.sendMessage).not.toHaveBeenCalled();
    });

    it('should not send starter when content is empty', () => {
      fixture.componentRef.setInput('assistantId', 'ast-123');
      fixture.componentRef.setInput('instructions', 'Some instructions');
      fixture.detectChanges();

      component.onStarterSelected('   ');

      expect(mockPreviewChatService.sendMessage).not.toHaveBeenCalled();
    });
  });

  describe('builtAssistant computed', () => {
    it('should return null when assistantId is missing', () => {
      fixture.componentRef.setInput('assistantId', null);
      fixture.componentRef.setInput('name', 'Test');
      fixture.detectChanges();
      expect(component.builtAssistant()).toBeNull();
    });

    it('should return null when name is empty', () => {
      fixture.componentRef.setInput('assistantId', 'ast-123');
      fixture.componentRef.setInput('name', '');
      fixture.detectChanges();
      expect(component.builtAssistant()).toBeNull();
    });

    it('should build assistant from current inputs', () => {
      fixture.componentRef.setInput('assistantId', 'ast-123');
      fixture.componentRef.setInput('name', 'My Bot');
      fixture.componentRef.setInput('description', 'A test bot');
      fixture.componentRef.setInput('instructions', 'Be helpful');
      fixture.componentRef.setInput('starters', ['Hi', 'Help me']);
      fixture.detectChanges();

      const built = component.builtAssistant();
      expect(built).not.toBeNull();
      expect(built!.assistantId).toBe('ast-123');
      expect(built!.name).toBe('My Bot');
      expect(built!.description).toBe('A test bot');
      expect(built!.instructions).toBe('Be helpful');
      expect(built!.starters).toEqual(['Hi', 'Help me']);
    });
  });

  describe('clearChat', () => {
    it('should delegate to service clearMessages', () => {
      component.clearChat();
      expect(mockPreviewChatService.clearMessages).toHaveBeenCalled();
    });
  });

  describe('onMessageCancelled', () => {
    it('should delegate to service cancelRequest', () => {
      component.onMessageCancelled();
      expect(mockPreviewChatService.cancelRequest).toHaveBeenCalled();
    });
  });
});
