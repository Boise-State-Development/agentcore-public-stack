import { NO_ERRORS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { beforeEach, describe, expect, it } from 'vitest';
import { AgentMentionService, MentionableAgent } from '../../../agents/services/agent-mention.service';
import { FileUploadService } from '../../../services/file-upload';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolService } from '../../../services/tool/tool.service';
import { VoiceChatService } from '../../services/voice';
import { ChatInputComponent } from './chat-input.component';

const AGENTS: MentionableAgent[] = [
  { agentId: 'a1', name: 'Alpha', group: 'own' },
  { agentId: 'a2', name: 'Bravo', group: 'own' },
  { agentId: 'a3', name: 'Charlie', group: 'pinned' },
];

class MentionServiceStub {
  readonly mentionable = signal<MentionableAgent[]>(AGENTS);
  readonly loading = signal(false);
  async load(): Promise<void> {}
  search(query: string): MentionableAgent[] {
    const needle = query.trim().toLowerCase();
    return this.mentionable().filter((agent) => agent.name.toLowerCase().startsWith(needle));
  }
}

describe('ChatInputComponent — the `@` menu keyboard path (D11)', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
          },
        },
        { provide: ToastService, useValue: { error: () => undefined, warning: () => undefined, info: () => undefined } },
        { provide: ToolService, useValue: {} },
        {
          provide: VoiceChatService,
          useValue: {
            status: signal('idle'),
            isVoiceActive: signal(false),
            agentTranscript: signal(''),
          },
        },
        { provide: SystemPromptsService, useValue: { activePrompt: signal(null) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
      ],
    })
      // The composer's child components (model dropdown, quota banners, file cards) drag in
      // their own service graphs and have nothing to do with the keyboard path under test.
      .overrideComponent(ChatInputComponent, {
        set: { imports: [], schemas: [NO_ERRORS_SCHEMA] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('showSettingsControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.detectChanges();

    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  });

  /** Type into the textarea the way the DOM does: value first, then the input event. */
  function type(value: string): void {
    textarea.value = value;
    textarea.setSelectionRange(value.length, value.length);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  /**
   * A key press as the browser delivers it — `keydown` *and* `keyup`. The keyup half is
   * the whole point: it is bound to the caret-move handler, and an unconditional token
   * resync there used to snap the highlight back to the first row.
   */
  function pressKey(key: string): void {
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key, cancelable: true, bubbles: true }));
    textarea.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
    fixture.detectChanges();
  }

  it('opens the menu on a word-initial `@`', () => {
    type('@');
    expect(component.isMentionMenuOpen()).toBe(true);
  });

  it('walks the list with ArrowDown and stays where it lands (regression: keyup reset)', () => {
    type('@');

    pressKey('ArrowDown');
    expect(component.mentionActiveIndex()).toBe(1);

    pressKey('ArrowDown');
    expect(component.mentionActiveIndex()).toBe(2);
  });

  it('wraps with ArrowUp from the first row', () => {
    type('@');
    pressKey('ArrowUp');
    expect(component.mentionActiveIndex()).toBe(AGENTS.length - 1);
  });

  it('resets the highlight when the query itself changes', () => {
    type('@');
    pressKey('ArrowDown');
    expect(component.mentionActiveIndex()).toBe(1);

    type('@B');
    expect(component.mentionActiveIndex()).toBe(0);
  });

  it('commits the highlighted agent on Enter rather than sending the message', () => {
    let submitted = false;
    component.messageSubmitted.subscribe(() => (submitted = true));

    type('@');
    pressKey('ArrowDown');
    pressKey('Enter');

    expect(submitted).toBe(false);
    expect(component.mentionedAgent()?.name).toBe('Bravo');
    expect(component.userInput()).toBe('@Bravo ');
  });
});

/**
 * Queue-instead-of-interrupt (kaizen 2026-08-28 #5).
 *
 * Enter used to route to Stop while a response was streaming, so a follow-up
 * typed out of habit killed the run the user was waiting on. And a send that
 * raced the single-flight guard cleared the composer before the 409 came back,
 * losing the text outright. Both are covered here.
 */
describe('ChatInputComponent — queueing a follow-up mid-stream', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;
  let submitted: { content: string; timestamp: Date; mentionAgentId?: string }[];
  let cancelled: number;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
          },
        },
        { provide: ToastService, useValue: { error: () => undefined, warning: () => undefined, info: () => undefined } },
        { provide: ToolService, useValue: {} },
        {
          provide: VoiceChatService,
          useValue: {
            status: signal('idle'),
            isVoiceActive: signal(false),
            agentTranscript: signal(''),
          },
        },
        { provide: SystemPromptsService, useValue: { activePrompt: signal(null) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
      ],
    })
      .overrideComponent(ChatInputComponent, {
        set: { imports: [], schemas: [NO_ERRORS_SCHEMA] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('showSettingsControl', false);
    fixture.componentRef.setInput('autoFocus', false);

    submitted = [];
    cancelled = 0;
    component.messageSubmitted.subscribe(m => submitted.push(m));
    component.messageCancelled.subscribe(() => cancelled++);

    fixture.detectChanges();
    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  });

  function type(value: string): void {
    textarea.value = value;
    textarea.setSelectionRange(value.length, value.length);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function pressEnter(): void {
    textarea.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true }),
    );
    fixture.detectChanges();
  }

  function setStreaming(streaming: boolean): void {
    fixture.componentRef.setInput('isChatLoading', streaming);
    fixture.detectChanges();
  }

  it('sends immediately when nothing is streaming', () => {
    type('first question');
    pressEnter();
    expect(submitted.map(m => m.content)).toEqual(['first question']);
  });

  it('queues instead of aborting the run when Enter lands mid-stream', () => {
    setStreaming(true);
    type('actually, use TypeScript');
    pressEnter();

    // The regression this replaces: Enter routed to Stop.
    expect(cancelled).toBe(0);
    expect(submitted).toEqual([]);
    expect(component.queuedMessages().map(q => q.content)).toEqual(['actually, use TypeScript']);
  });

  it('clears the composer on queue so the next follow-up can be typed', () => {
    setStreaming(true);
    type('one');
    pressEnter();
    expect(component.userInput()).toBe('');

    type('two');
    pressEnter();
    expect(component.queuedMessages().map(q => q.content)).toEqual(['one', 'two']);
  });

  it('sends exactly one queued message when the turn finishes', () => {
    setStreaming(true);
    type('one');
    pressEnter();
    type('two');
    pressEnter();

    setStreaming(false);

    // One per completion — the backend serialises turns per session, so
    // flushing both at once would just collide with the single-flight guard.
    expect(submitted.map(m => m.content)).toEqual(['one']);
    expect(component.queuedMessages().map(q => q.content)).toEqual(['two']);
  });

  it('drains the rest of the queue across subsequent completions, in order', () => {
    setStreaming(true);
    type('one');
    pressEnter();
    type('two');
    pressEnter();

    setStreaming(false);
    setStreaming(true);
    setStreaming(false);

    expect(submitted.map(m => m.content)).toEqual(['one', 'two']);
    expect(component.queuedMessages()).toEqual([]);
  });

  it('flushes after an aborted or failed turn too, not just a clean finish', () => {
    // The composer cannot tell done from abort from error — it only sees
    // loading go false. Reading the flush as an invariant rather than a
    // transition is what makes all three paths behave the same.
    setStreaming(true);
    type('follow-up');
    pressEnter();

    component.cancelChatRequest();
    setStreaming(false);

    expect(cancelled).toBe(1);
    expect(submitted.map(m => m.content)).toEqual(['follow-up']);
  });

  it('stamps the timestamp at send time, not queue time', () => {
    setStreaming(true);
    type('later');
    pressEnter();
    const queuedAt = Date.now();

    setStreaming(false);

    // Ordering in the message list is by timestamp; stamping at queue time
    // would sort this ahead of the reply that was still streaming.
    expect(submitted[0].timestamp.getTime()).toBeGreaterThanOrEqual(queuedAt);
  });

  it('keeps the button on Stop while streaming', () => {
    setStreaming(true);
    type('typed but not sent');

    component.onPrimaryButtonClick();

    expect(cancelled).toBe(1);
    expect(submitted).toEqual([]);
    // The text is untouched — stopping is not sending.
    expect(component.userInput()).toBe('typed but not sent');
  });

  it('lets a queued message be taken back before it sends', () => {
    setStreaming(true);
    type('one');
    pressEnter();
    type('two');
    pressEnter();

    component.removeQueuedMessage(0);
    setStreaming(false);

    expect(submitted.map(m => m.content)).toEqual(['two']);
  });

  it('refuses to queue an empty composer', () => {
    setStreaming(true);
    type('   ');
    pressEnter();
    expect(component.queuedMessages()).toEqual([]);
  });

  it('carries the turn-scoped @-mention with the queued message', () => {
    setStreaming(true);
    type('@Alpha');
    fixture.detectChanges();
    pressEnter(); // commits the mention from the open menu
    type('@Alpha check this');
    pressEnter(); // queues

    setStreaming(false);

    expect(submitted[0].mentionAgentId).toBe('a1');
  });
});
