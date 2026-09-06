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
import { SteeringService } from '../../services/chat/steering.service';
import { ChatInputComponent } from './chat-input.component';

const AGENTS: MentionableAgent[] = [
  { agentId: 'a1', name: 'Alpha', group: 'own' },
  { agentId: 'a2', name: 'Bravo', group: 'own' },
  { agentId: 'a3', name: 'Charlie', group: 'pinned' },
];

/**
 * Stand-in for SteeringService: same surface, no HTTP.
 *
 * A DI token rather than a `vi.mock`, per the house rule — module mocking
 * leaks across spec files, and the real service reaches SessionService →
 * HttpClient, which these specs have no business standing up.
 */
class SteeringServiceStub {
  readonly applied = signal<string[]>([]);
  /** Set by a test to decide whether the backend "armed" the entry. */
  armResult = true;
  toolsInUse = false;
  /** Signal, not a plain field: the hold has to re-run the flush effect when
   *  the prompt clears, or a dismissed prompt would strand the queue. */
  readonly held = signal(false);
  published: { id: string; text: string }[] = [];
  readonly armCalls: { sessionId: string; entryId: string; text: string }[] = [];
  readonly withdrawCalls: { sessionId: string; entryId: string }[] = [];

  canSteer(sessionId: string | null): boolean {
    return !!sessionId && this.toolsInUse;
  }
  markToolUsed(): void {}
  startTurn(): void {}
  async arm(sessionId: string, entryId: string, text: string): Promise<boolean> {
    this.armCalls.push({ sessionId, entryId, text });
    return this.armResult;
  }
  async withdraw(sessionId: string, entryId: string): Promise<void> {
    this.withdrawCalls.push({ sessionId, entryId });
  }
  shouldHoldQueue(): boolean {
    return this.held();
  }
  publishQueue(_sessionId: string | null, entries: { id: string; text: string }[]): void {
    this.published = entries;
  }
  carriedFor(): { id: string; text: string }[] {
    return this.published;
  }
  recordApplied(): void {}
  consumeApplied(entryId: string): void {
    this.applied.update((ids) => ids.filter((id) => id !== entryId));
  }
  reset(): void {}
}

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
        { provide: SteeringService, useClass: SteeringServiceStub },
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
        { provide: SteeringService, useClass: SteeringServiceStub },
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


describe('ChatInputComponent — mid-turn steering (PR-5)', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;
  let steering: SteeringServiceStub;
  let readyUploadIds: ReturnType<typeof signal<string[]>>;
  let submitted: { content: string }[];

  beforeEach(async () => {
    readyUploadIds = signal<string[]>([]);
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds,
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
        { provide: SteeringService, useClass: SteeringServiceStub },
      ],
    })
      .overrideComponent(ChatInputComponent, {
        set: { imports: [], schemas: [NO_ERRORS_SCHEMA] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    steering = TestBed.inject(SteeringService) as unknown as SteeringServiceStub;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('showSettingsControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.componentRef.setInput('sessionId', 'sess-1');

    submitted = [];
    component.messageSubmitted.subscribe((m) => submitted.push(m));

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

  /** Let the fire-and-forget arm request settle. */
  const settle = () => Promise.resolve().then(() => undefined);

  it('arms a queued follow-up against the running turn', async () => {
    setStreaming(true);
    type('use the other file');
    pressEnter();
    await settle();

    expect(steering.armCalls).toHaveLength(1);
    expect(steering.armCalls[0].sessionId).toBe('sess-1');
    expect(steering.armCalls[0].text).toBe('use the other file');
    // The id armed on the backend is the id on the queued entry — that shared
    // id is what makes the ack able to name exactly one composer entry.
    expect(steering.armCalls[0].entryId).toBe(component.queuedMessages()[0].id);
  });

  it('marks the entry armed once the backend confirms', async () => {
    setStreaming(true);
    type('hi');
    pressEnter();
    await settle();
    fixture.detectChanges();

    expect(component.queuedMessages()[0].armed).toBe(true);
  });

  it('leaves the entry unarmed when there was no live turn to steer', async () => {
    steering.armResult = false;
    setStreaming(true);
    type('too late');
    pressEnter();
    await settle();

    // Not an error: it stays queued and the falling edge sends it normally.
    expect(component.queuedMessages()[0].armed).toBeUndefined();
    setStreaming(false);
    expect(submitted.map((m) => m.content)).toEqual(['too late']);
  });

  it('does not arm a follow-up carrying file attachments', async () => {
    readyUploadIds.set(['upload-1']);
    setStreaming(true);
    type('look at this');
    pressEnter();
    await settle();

    // An injection is a text block on the tool-result message; it cannot carry
    // files. Skipping the round trip is what makes that automatic instead of a
    // backend rejection.
    expect(steering.armCalls).toEqual([]);
  });

  it('does not arm a follow-up carrying an @-mention', async () => {
    setStreaming(true);
    type('@Alpha');
    fixture.detectChanges();
    pressEnter();
    type('@Alpha check this');
    pressEnter();
    await settle();

    // A mention picks the Agent that runs a *turn*; a mid-turn injection
    // cannot change which agent is already running.
    expect(steering.armCalls).toEqual([]);
  });

  it('drops the entry when the backend acks the injection', async () => {
    setStreaming(true);
    type('use the other file');
    pressEnter();
    await settle();
    const entryId = component.queuedMessages()[0].id;

    steering.applied.set([entryId]);
    fixture.detectChanges();

    expect(component.queuedMessages()).toEqual([]);
  });

  it('does not also send an acked follow-up at the end of the turn', async () => {
    setStreaming(true);
    type('use the other file');
    pressEnter();
    await settle();

    steering.applied.set([component.queuedMessages()[0].id]);
    fixture.detectChanges();
    setStreaming(false);

    // The whole point: injected once, not injected and then sent again.
    expect(submitted).toEqual([]);
  });

  it('ignores an ack for an entry it does not hold', async () => {
    setStreaming(true);
    type('mine');
    pressEnter();
    await settle();

    steering.applied.set(['some-other-composers-entry']);
    fixture.detectChanges();

    expect(component.queuedMessages().map((q) => q.content)).toEqual(['mine']);
  });

  it('withdraws an entry the user removes from the composer', async () => {
    setStreaming(true);
    type('never mind');
    pressEnter();
    await settle();
    const entryId = component.queuedMessages()[0].id;

    component.removeQueuedMessage(0);

    expect(steering.withdrawCalls).toEqual([{ sessionId: 'sess-1', entryId }]);
    expect(component.queuedMessages()).toEqual([]);
  });

  it('keeps a removed entry removed even if its arm lands afterwards', async () => {
    setStreaming(true);
    type('never mind');
    pressEnter();
    component.removeQueuedMessage(0);
    await settle();
    fixture.detectChanges();

    // The arm resolving must not resurrect an entry the user already took
    // back — it re-reads the queue rather than closing over the entry.
    expect(component.queuedMessages()).toEqual([]);
  });

  it('promises end-of-turn delivery until the turn has used a tool', () => {
    setStreaming(true);
    fixture.detectChanges();
    expect(textarea.getAttribute('placeholder')).toContain('when this response finishes');
  });

  it('promises next-step delivery once the turn has tool boundaries', () => {
    steering.toolsInUse = true;
    setStreaming(true);
    fixture.detectChanges();
    // A turn that calls no tools has nowhere to put an injection, so
    // over-promising here is the failure that teaches users not to trust it.
    expect(textarea.getAttribute('placeholder')).toContain('at the next step');
  });

  it('goes back to the idle placeholder when nothing is streaming', () => {
    steering.toolsInUse = true;
    setStreaming(false);
    expect(textarea.getAttribute('placeholder')).toBe('How can I help you today?');
  });
});


describe('ChatInputComponent — a queue held behind a paused turn (PR-6)', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;
  let steering: SteeringServiceStub;
  let submitted: { content: string }[];

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
        { provide: SteeringService, useClass: SteeringServiceStub },
      ],
    })
      .overrideComponent(ChatInputComponent, {
        set: { imports: [], schemas: [NO_ERRORS_SCHEMA] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    steering = TestBed.inject(SteeringService) as unknown as SteeringServiceStub;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('showSettingsControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.componentRef.setInput('sessionId', 'sess-1');

    submitted = [];
    component.messageSubmitted.subscribe((m) => submitted.push(m));

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

  /** A turn that pauses: the stream closes (loading falls) with a prompt up. */
  function pauseTurn(): void {
    steering.held.set(true);
    setStreaming(false);
  }

  it('queues a follow-up typed AFTER the pause, rather than sending it', () => {
    // The sequence a real user performs, and the one the original PR-6 tests
    // missed: they queued while streaming and then paused. A pause closes the
    // stream, so `isChatLoading` is already false by the time the prompt is on
    // screen — and gating the queue on loading alone routed Enter straight to
    // `submitChatRequest`, firing a brand-new turn that abandoned the paused
    // one. Observed live on dev.
    pauseTurn();

    type('after the greeting, also tell me the day');
    pressEnter();

    expect(submitted).toEqual([]);
    expect(component.queuedMessages().map((q) => q.content)).toEqual([
      'after the greeting, also tell me the day',
    ]);
  });

  it('the Send button makes the same decision as Enter while held', () => {
    // Two affordances that disagree about what "send" means is worse than
    // either behaviour on its own.
    pauseTurn();

    type('via the button');
    component.onPrimaryButtonClick();

    expect(submitted).toEqual([]);
    expect(component.queuedMessages().map((q) => q.content)).toEqual(['via the button']);
  });

  it('does not try to arm an entry queued while paused', async () => {
    // The paused turn released its lease when the stream closed, so there is
    // no inbox to arm against — the entry rides the resume request instead.
    pauseTurn();

    type('rides the resume');
    pressEnter();
    await Promise.resolve();

    expect(steering.armCalls).toEqual([]);
    expect(steering.published.map((e) => e.text)).toEqual(['rides the resume']);
  });

  it('still sends immediately when nothing is paused and nothing is streaming', () => {
    // The ordinary idle path must not become a queue.
    steering.held.set(false);
    setStreaming(false);

    type('just a normal message');
    pressEnter();

    expect(submitted.map((m) => m.content)).toEqual(['just a normal message']);
    expect(component.queuedMessages()).toEqual([]);
  });

  it('does not send a follow-up while a prompt is awaiting an answer', () => {
    setStreaming(true);
    type('actually use the local file');
    pressEnter();

    pauseTurn();

    // Sending here starts a NEW turn, which abandons the paused one the user
    // is in the middle of answering — and can race the resume that follows
    // into the single-flight guard.
    expect(submitted).toEqual([]);
    expect(component.queuedMessages().map((q) => q.content)).toEqual([
      'actually use the local file',
    ]);
  });

  it('flushes as soon as the prompt is dismissed', () => {
    setStreaming(true);
    type('never mind the calendar');
    pressEnter();
    pauseTurn();

    steering.held.set(false);
    fixture.detectChanges();

    // The hold must be bounded by an action the user already has, or a prompt
    // they never answer strands the follow-up forever.
    expect(submitted.map((m) => m.content)).toEqual(['never mind the calendar']);
  });

  it('publishes the queue so the resume request can carry it', () => {
    setStreaming(true);
    type('one');
    pressEnter();
    type('two');
    pressEnter();

    expect(steering.published.map((e) => e.text)).toEqual(['one', 'two']);
    expect(steering.published.map((e) => e.id)).toEqual(
      component.queuedMessages().map((q) => q.id),
    );
  });

  it('stops publishing an entry once it is acked', async () => {
    setStreaming(true);
    type('use the other file');
    pressEnter();
    await Promise.resolve();

    steering.applied.set([component.queuedMessages()[0].id]);
    fixture.detectChanges();

    // A carried entry the resumed turn already injected must not be carried
    // again by a second resume.
    expect(steering.published).toEqual([]);
  });

  it('explains the hold in the placeholder', () => {
    pauseTurn();
    // The turn is not streaming while it is paused, so without this the idle
    // placeholder would promise immediate delivery on the slowest path.
    expect(textarea.getAttribute('placeholder')).toContain('when you answer above');
  });

  it('still flushes normally when nothing is paused', () => {
    setStreaming(true);
    type('ordinary follow-up');
    pressEnter();
    setStreaming(false);

    expect(submitted.map((m) => m.content)).toEqual(['ordinary follow-up']);
  });
});
