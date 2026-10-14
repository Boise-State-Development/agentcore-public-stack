import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, input, output } from '@angular/core';
import { By } from '@angular/platform-browser';

import { ChatContainerComponent } from './chat-container.component';
import { ChatInputComponent, ComposerProject } from '../chat-input/chat-input.component';
import { AgentLaunchCardComponent } from '../../../agents/components/agent-launch-card.component';
import { Assistant } from '../../../assistants/models/assistant.model';

/**
 * A project task's empty state: the greeting, with the composer naming the
 * project, never the harness Agent's launch card. The composer and the card
 * are stubbed — this is about which layout the container picks and what it
 * hands the composer, not about either one's own rendering.
 */
describe('ChatContainerComponent — a project conversation', () => {
  @Component({ selector: 'app-chat-input', template: '' })
  class ChatInputStub {
    readonly sessionId = input<string | null>(null);
    readonly allowFolderAttachments = input<boolean>(false);
    readonly showScopePicker = input<boolean>(false);
    readonly project = input<ComposerProject | null>(null);
    readonly draftKey = input<string | null>(null);
    readonly isChatLoading = input<boolean>(false);
    readonly showFileControls = input<boolean>(true);
    readonly showVoiceControl = input<boolean>(true);
    readonly showAnnouncements = input<boolean>(true);
    readonly announcementPlacement = input<'above' | 'below'>('above');
    readonly messageSubmitted = output<{ content: string; timestamp: Date }>();
    readonly messageCancelled = output<void>();
    readonly fileAttached = output<File>();
  }

  @Component({ selector: 'app-agent-launch-card', template: '' })
  class LaunchCardStub {
    readonly view = input<unknown>(null);
    readonly runnability = input<unknown>(null);
    readonly closable = input(false);
    readonly starterSelected = output<string>();
    readonly closed = output<void>();
  }

  const PROJECT: ComposerProject = { projectId: 'p1', name: 'Biology 101' };

  const HARNESS = {
    assistantId: 'harness-1',
    name: 'Biology 101',
    description: '',
    ownerName: 'Pat',
    starters: [],
    kind: 'project',
    projectId: 'p1',
  } as unknown as Assistant;

  let fixture: ComponentFixture<ChatContainerComponent>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    TestBed.overrideComponent(ChatContainerComponent, {
      remove: { imports: [ChatInputComponent, AgentLaunchCardComponent] },
      add: { imports: [ChatInputStub, LaunchCardStub] },
    });
    fixture = TestBed.createComponent(ChatContainerComponent);
    fixture.componentRef.setInput('messages', []);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function render(inputs: Record<string, unknown>): HTMLElement {
    for (const [name, value] of Object.entries(inputs)) {
      fixture.componentRef.setInput(name, value);
    }
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function composer(): ChatInputStub {
    return fixture.debugElement.query(By.directive(ChatInputStub)).componentInstance as ChatInputStub;
  }

  it('shows the harness Agent as a launch card when it is not a project', () => {
    // The control: an ordinary Agent keeps its card.
    const html = render({ assistant: { ...HARNESS, kind: undefined, projectId: undefined } });
    expect(html.querySelector('app-agent-launch-card')).not.toBeNull();
    expect(html.querySelector('[data-testid="greeting-message"]')).toBeNull();
  });

  it('shows the greeting and no launch card for a project', () => {
    const html = render({ assistant: HARNESS, project: PROJECT });
    expect(html.querySelector('app-agent-launch-card')).toBeNull();
    expect(html.querySelector('[data-testid="greeting-message"]')).not.toBeNull();
  });

  it('hands the composer the project', () => {
    render({ assistant: HARNESS, project: PROJECT });
    expect(composer().project()).toEqual(PROJECT);
  });

  it('skips the card-shaped skeleton while a project harness loads', () => {
    // The session page resolves the project from the URL before the harness
    // lands, so the layout settles on the greeting rather than flashing a card.
    const html = render({ isLoadingAssistant: true, project: PROJECT });
    // The skeleton card, not the greeting's own typing cursor (also a pulse).
    expect(html.querySelector('div.animate-pulse.rounded-2xl')).toBeNull();
    expect(html.querySelector('[data-testid="greeting-message"]')).not.toBeNull();
    expect(composer().project()).toEqual(PROJECT);
  });
});
