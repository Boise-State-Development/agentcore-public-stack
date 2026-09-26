import { computed, NO_ERRORS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentMentionService, MentionableAgent } from '../../../agents/services/agent-mention.service';
import { SkillCommand, SkillCommandService } from '../../../services/skill/skill-command.service';
import { FileMetadata, FileUploadService } from '../../../services/file-upload';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolService } from '../../../services/tool/tool.service';
import { VoiceChatService } from '../../services/voice';
import {
  DictationService,
  DictationUnavailableError,
  type DictationEndReason,
  type DictationHandlers,
  type DictationStatus,
} from '../../services/dictation';
import { SteeringService } from '../../services/chat/steering.service';
import { ComposerDraftService } from '../../services/session/composer-draft.service';
import { NEW_CONVERSATION_DRAFT_KEY } from '../../services/session/composer-draft-storage.service';
import { ChatInputComponent, spliceDictation } from './chat-input.component';
import { ComposerHandoffService } from './composer-handoff.service';

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

const SKILLS: SkillCommand[] = [
  { skillId: 's1', slug: 'brand-deck', name: 'Brand Deck', description: 'Build a deck' },
  { skillId: 's2', slug: 'brand-voice', name: 'Brand Voice', description: 'House style' },
  { skillId: 's3', slug: 'web-research', name: 'Web Research', description: 'Search the web' },
];

/**
 * Stand-in for SkillCommandService. A DI token rather than a `vi.mock`, per the house
 * rule — and required rather than optional, because the real one reaches SkillService →
 * HttpClient, which Angular 21 root-provides a live backend for.
 */
class SkillCommandServiceStub {
  readonly commands = signal<SkillCommand[]>(SKILLS);
  readonly loading = signal(false);
  async load(): Promise<void> {}
  search(query: string): SkillCommand[] {
    const needle = query.trim().toLowerCase();
    return this.commands().filter((command) => command.slug.startsWith(needle));
  }
  bySlugOrUndefined(slug: string): SkillCommand | undefined {
    return this.commands().find((command) => command.slug === slug);
  }
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
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
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

  it('un-mentions once the @Name is deleted, since the highlighted name is the only indicator', () => {
    type('@');
    pressKey('Enter');
    expect(component.mentionedAgent()?.name).toBe('Alpha');

    type('@Alp');
    expect(component.mentionedAgent()).toBeNull();
  });

  it('keeps the mention while the @Name is still in the text', () => {
    type('@');
    pressKey('Enter');
    type('@Alpha please summarise this');
    expect(component.mentionedAgent()?.name).toBe('Alpha');
  });
});

/**
 * The `/` skill-command menu.
 *
 * Two things separate it from the `@` menu and both are covered here. First, `/` is
 * ordinary punctuation — dates, fractions, paths and URLs all contain one — so the token
 * rule has to keep the menu shut far more often than it opens it. Second, there is no
 * remembered pick: the invoked set is DERIVED from the composer text, so a hand-typed
 * command works exactly like a menu pick and the chip can never disagree with what is
 * about to be sent.
 */
describe('ChatInputComponent — the `/` skill-command menu', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
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
    fixture.componentRef.setInput('autoFocus', false);
    fixture.detectChanges();

    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  });

  function type(value: string): void {
    textarea.value = value;
    textarea.setSelectionRange(value.length, value.length);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function pressKey(key: string): void {
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key, cancelable: true, bubbles: true }));
    textarea.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
    fixture.detectChanges();
  }

  it('opens the menu on a word-initial `/`', () => {
    type('/');
    expect(component.isSkillMenuVisible()).toBe(true);
  });

  it('filters as the slug is typed', () => {
    type('/brand-v');
    expect(component.skillResults().map((c) => c.slug)).toEqual(['brand-voice']);
  });

  it.each(['and/or', 'open 24/7', 'see https://example.com', 'edit src/app/foo'])(
    'leaves `/` as punctuation in %j',
    (text) => {
      type(text);
      expect(component.isSkillMenuVisible()).toBe(false);
    },
  );

  it('closes once a second `/` makes the token a path', () => {
    type('/usr');
    expect(component.isSkillMenuVisible()).toBe(true);

    type('/usr/');
    expect(component.isSkillMenuVisible()).toBe(false);
  });

  it('walks the list and commits on Enter rather than sending the message', () => {
    let submitted = false;
    component.messageSubmitted.subscribe(() => (submitted = true));

    type('/');
    pressKey('ArrowDown');
    pressKey('Enter');

    expect(submitted).toBe(false);
    expect(component.userInput()).toBe('/brand-voice ');
  });

  it('yields the keyboard to the `@` menu when both tokens would match', () => {
    // `@` is the narrower token; two menus claiming the arrow keys would make neither
    // usable.
    type('@');
    expect(component.isMentionMenuOpen()).toBe(true);
    expect(component.isSkillMenuVisible()).toBe(false);
  });

  it('derives the invoked skills from the text, so a typed command counts', () => {
    type('use /web-research to check this');
    expect(component.invokedSkills().map((c) => c.skillId)).toEqual(['s3']);
  });

  it('ignores a slug the user has not turned on', () => {
    type('run /not-a-skill please');
    expect(component.invokedSkills()).toEqual([]);
  });

  it('de-duplicates a slug repeated in one message', () => {
    type('/brand-deck and again /brand-deck');
    expect(component.invokedSkills().map((c) => c.slug)).toEqual(['brand-deck']);
  });

  it('sends the derived ids with the message', () => {
    let payload: { invokedSkillIds?: string[] } | undefined;
    component.messageSubmitted.subscribe((event) => (payload = event));

    type('/brand-deck /web-research make me a deck');
    component.submitChatRequest();

    expect(payload?.invokedSkillIds).toEqual(['s1', 's3']);
  });

  it('omits the field entirely when no command was used', () => {
    let payload: { invokedSkillIds?: string[] } | undefined;
    component.messageSubmitted.subscribe((event) => (payload = event));

    type('just a normal message');
    component.submitChatRequest();

    expect(payload?.invokedSkillIds).toBeUndefined();
  });

  it('tints the command in place, and deleting it un-invokes, because the text is the binding', () => {
    type('/brand-deck make me a deck');
    const marked = component['highlightSegments']()?.filter((segment) => segment.mark);
    expect(marked?.map((segment) => segment.text)).toEqual(['/brand-deck']);

    type('make me a deck');
    expect(component.invokedSkills()).toEqual([]);
    expect(component['highlightSegments']()).toBeNull();
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
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
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

  function button(label: string): HTMLButtonElement | null {
    return fixture.nativeElement.querySelector(`button[aria-label="${label}"]`);
  }

  it('has no send button on a keyboard device — Enter sends', () => {
    type('something to send');
    expect(button('Send message')).toBeNull();
    expect(button('Stop response')).toBeNull();
  });

  it('shows Stop only while streaming, and it stops without sending the typed text', () => {
    setStreaming(true);
    type('typed but not sent');

    button('Stop response')!.click();

    expect(cancelled).toBe(1);
    expect(submitted).toEqual([]);
    // The text is untouched — stopping is not sending.
    expect(component.userInput()).toBe('typed but not sent');

    setStreaming(false);
    expect(button('Stop response')).toBeNull();
  });

  it('stops on Escape while streaming', () => {
    setStreaming(true);
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true }));
    expect(cancelled).toBe(1);
  });

  it('ignores Escape when nothing is streaming', () => {
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true }));
    expect(cancelled).toBe(0);
  });

  it('says how to stop on the status line while a compact composer streams', () => {
    fixture.componentRef.setInput('compact', true);
    expect(component['statusHint']()).toBeNull();
    setStreaming(true);
    expect(component['statusHint']()).toBe('Esc to stop');
  });

  it('shows no stop hint under the empty-state composer, which is swapped out on send', () => {
    setStreaming(true);
    expect(component['statusHint']()).toBeNull();
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
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds,
            // Kept in step with the ids: the composer reads the full upload to
            // record an attachment's filename and size in the draft.
            readyUploads: computed(() =>
              readyUploadIds().map((uploadId) => ({
                uploadId,
                file: new File(['x'], `${uploadId}.pdf`, { type: 'application/pdf' }),
                status: 'ready' as const,
                progress: 100,
              })),
            ),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
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
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
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

  it('the touch Send button makes the same decision as Enter while held', () => {
    // Two affordances that disagree about what "send" means is worse than
    // either behaviour on its own.
    component['isCoarsePointer'].set(true);
    pauseTurn();

    type('via the button');
    (fixture.nativeElement.querySelector('button[aria-label="Send message"]') as HTMLButtonElement).click();

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


/**
 * The rotating discovery hints in the empty composer.
 *
 * The contract worth pinning is the accessibility one: the *visible* line
 * cycles, the *accessible* placeholder never does. Everything else here exists
 * so the rotation cannot quietly turn into something that runs forever.
 */
describe('ChatInputComponent — rotating discovery hints', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let textarea: HTMLTextAreaElement;
  let mentions: MentionServiceStub;
  let skills: SkillCommandServiceStub;

  /**
   * The line the overlay is painting, or null when the composer has handed the
   * placeholder back to the textarea. Excludes a node still playing its exit
   * animation, which briefly shares the DOM with its replacement.
   */
  function hint(): HTMLElement | null {
    const nodes = fixture.nativeElement.querySelectorAll(
      '.composer-hint:not(.composer-hint-leave)',
    );
    return (nodes[nodes.length - 1] as HTMLElement | undefined) ?? null;
  }

  function hintText(): string | null {
    return hint()?.textContent?.trim() ?? null;
  }

  function tick(): void {
    vi.advanceTimersByTime(4500);
    fixture.detectChanges();
  }

  beforeEach(async () => {
    vi.useFakeTimers();
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
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
    mentions = TestBed.inject(AgentMentionService) as unknown as MentionServiceStub;
    skills = TestBed.inject(SkillCommandService) as unknown as SkillCommandServiceStub;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.detectChanges();

    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('paints the idle line first, hidden from assistive tech', () => {
    expect(hintText()).toBe('How can I help you today?');
    expect(hint()?.getAttribute('aria-hidden')).toBe('true');
  });

  it('cycles through the shortcuts a user cannot otherwise discover', () => {
    tick();
    expect(hintText()).toContain('Type @');

    tick();
    expect(hintText()).toContain('Type /');
  });

  it('leaves the accessible placeholder alone while the pixels change', () => {
    tick();
    // The overlay is decoration. A placeholder that rotated with it would
    // re-announce the field every few seconds.
    expect(textarea.getAttribute('placeholder')).toBe('How can I help you today?');
  });

  it('cycles more than once — a single pass is over before anyone looks', () => {
    // 3 hints x 3 passes. Sampling the head of the second pass is what would
    // catch a regression back to stopping after one.
    for (let i = 0; i < 3; i++) tick();
    expect(hintText()).toBe('How can I help you today?');

    tick();
    expect(hintText()).toContain('Type @');
  });

  it('comes to rest on the idle line instead of looping forever', () => {
    // Nothing on screen may auto-update indefinitely without a pause control,
    // so the passes run out and the composer settles.
    for (let i = 0; i < 9; i++) tick();
    expect(hintText()).toBe('How can I help you today?');

    for (let i = 0; i < 4; i++) tick();
    expect(hintText()).toBe('How can I help you today?');
  });

  it('rests in the overlay rather than handing back to the placeholder', () => {
    for (let i = 0; i < 10; i++) tick();

    // Unmounting at rest would exit-animate a copy of the idle line straight
    // off the native placeholder, which spells the same words.
    expect(hint()).not.toBeNull();
    expect(textarea.className).toContain('placeholder:text-transparent');
  });

  it('stops the moment the user starts typing', () => {
    textarea.value = 'h';
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'h', bubbles: true }));
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    expect(hint()).toBeNull();

    // And it does not pick back up where it left off if the box is emptied —
    // a hint that returned every time would be pestering, not teaching.
    textarea.value = '';
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    tick();
    expect(hintText()).toBe('How can I help you today?');
  });

  it('starts over on a brand-new conversation', () => {
    textarea.value = 'h';
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    textarea.value = '';
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    fixture.componentRef.setInput('sessionId', 'sess-1');
    fixture.detectChanges();
    fixture.componentRef.setInput('sessionId', null);
    fixture.detectChanges();

    // An empty composer on a new conversation is the one moment the hints are
    // worth showing again.
    tick();
    expect(hintText()).toContain('Type @');
  });

  it('never advertises a shortcut this composer does not offer', () => {
    mentions.mentionable.set([]);
    skills.commands.set([]);
    fixture.detectChanges();

    // With nothing left to teach, the idle string alone is not worth animating
    // — and a `/` hint for a user with no skills opens an empty menu.
    expect(hint()).toBeNull();
    expect(textarea.getAttribute('placeholder')).toBe('How can I help you today?');
  });

  it('yields to the mid-stream placeholder while a turn is running', () => {
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.detectChanges();

    // The streaming placeholder says where a follow-up will land. That is
    // state, and it outranks a discovery hint.
    expect(hint()).toBeNull();
    expect(textarea.getAttribute('placeholder')).toContain('when this response finishes');
  });
});

describe('ChatInputComponent — composer drafts (feedback retry-with-correction)', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
          },
        },
        { provide: ToastService, useValue: { error: () => undefined, warning: () => undefined, info: () => undefined } },
        { provide: ToolService, useValue: {} },
        {
          provide: VoiceChatService,
          useValue: { status: signal('idle'), isVoiceActive: signal(false), agentTranscript: signal('') },
        },
        { provide: SystemPromptsService, useValue: { activePrompt: signal(null) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
        { provide: SteeringService, useClass: SteeringServiceStub },
      ],
    })
      .overrideComponent(ChatInputComponent, { set: { imports: [], schemas: [NO_ERRORS_SCHEMA] } })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.componentRef.setInput('sessionId', 's1');
    fixture.detectChanges();
  });

  it('takes a draft for its own session into the textarea without submitting', () => {
    const drafts = TestBed.inject(ComposerDraftService);
    let submitted = 0;
    component.messageSubmitted.subscribe(() => submitted++);

    drafts.request('s1', 'That answer ignored my instructions. ');
    fixture.detectChanges();

    expect(component.userInput()).toBe('That answer ignored my instructions. ');
    const textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea.value).toBe('That answer ignored my instructions. ');
    expect(drafts.pending()).toBeNull();
    expect(submitted).toBe(0);
  });

  it('leaves another session\'s draft alone', () => {
    const drafts = TestBed.inject(ComposerDraftService);
    drafts.request('s2', 'not mine');
    fixture.detectChanges();
    expect(component.userInput()).toBe('');
    expect(drafts.pending()?.text).toBe('not mine');
  });
});

describe('ChatInputComponent — unsent text survives leaving the conversation', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;

  /** Live "ready" uploads the stubbed FileUploadService reports, per test. */
  let stubReadyUploads: { uploadId: string; file: File; status: 'ready'; progress: number }[];
  /** What `GET /files?sessionId=` answers, and what it was asked about. */
  let stubSessionFiles: FileMetadata[];
  let listedSessions: string[];
  let listSessionFilesImpl: (sessionId: string) => Promise<FileMetadata[]>;

  function serverFile(uploadId: string, sessionId = 'staged-1'): FileMetadata {
    return {
      uploadId,
      filename: `${uploadId}.pdf`,
      mimeType: 'application/pdf',
      sizeBytes: 2048,
      sessionId,
      s3Uri: `s3://bucket/${uploadId}`,
      status: 'ready',
      createdAt: '2026-09-21T00:00:00Z',
    };
  }

  function readyUpload(uploadId: string) {
    return {
      uploadId,
      file: new File(['x'], `${uploadId}.pdf`, { type: 'application/pdf' }),
      status: 'ready' as const,
      progress: 100,
    };
  }

  async function mount(draftKey: string | null): Promise<void> {
    TestBed.resetTestingModule();
    const readyUploads = signal(stubReadyUploads);
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: readyUploads,
            hasActivePendingUploads: signal(false),
            readyUploadIds: computed(() => readyUploads().map((u) => u.uploadId)),
            readyUploads,
            clearReadyUploads: () => readyUploads.set([]),
            clearPendingUpload: (uploadId: string) =>
              readyUploads.update((list) => list.filter((u) => u.uploadId !== uploadId)),
            listSessionFiles: (sessionId: string) => {
              listedSessions.push(sessionId);
              return listSessionFilesImpl(sessionId);
            },
          },
        },
        { provide: ToastService, useValue: { error: () => undefined, warning: () => undefined, info: () => undefined } },
        { provide: ToolService, useValue: {} },
        {
          provide: VoiceChatService,
          useValue: { status: signal('idle'), isVoiceActive: signal(false), agentTranscript: signal('') },
        },
        { provide: SystemPromptsService, useValue: { activePrompt: signal(null) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
        { provide: SteeringService, useClass: SteeringServiceStub },
      ],
    })
      .overrideComponent(ChatInputComponent, { set: { imports: [], schemas: [NO_ERRORS_SCHEMA] } })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.componentRef.setInput('sessionId', draftKey);
    fixture.componentRef.setInput('draftKey', draftKey);
    fixture.detectChanges();
    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  }

  /** Type into the textarea the way the DOM does. */
  function type(value: string): void {
    textarea.value = value;
    textarea.setSelectionRange(value.length, value.length);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  /** Let the background reconcile resolve, then flush what it changed. */
  async function settleMicrotasks(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();
  }

  beforeEach(() => {
    localStorage.clear();
    stubReadyUploads = [];
    stubSessionFiles = [];
    listedSessions = [];
    listSessionFilesImpl = async () => stubSessionFiles;
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('reinstates the draft when the composer comes back to the conversation', async () => {
    await mount('s1');
    type('where was I');

    // Navigating away and back: the component is torn down and rebuilt.
    await mount('s1');

    expect(component.userInput()).toBe('where was I');
    expect(textarea.value).toBe('where was I');
  });

  it('opens a conversation the user never typed in with an empty composer', async () => {
    await mount('s1');
    type('only in s1');

    await mount('s2');

    expect(component.userInput()).toBe('');
  });

  it('files the outgoing text under the conversation being left, not the one arriving', async () => {
    await mount('s1');
    type('belongs to s1');

    // The live swap: one component instance, `draftKey` changing underneath it.
    fixture.componentRef.setInput('draftKey', 's2');
    fixture.detectChanges();
    expect(component.userInput()).toBe('');

    fixture.componentRef.setInput('draftKey', 's1');
    fixture.detectChanges();
    expect(component.userInput()).toBe('belongs to s1');
  });

  it('swaps in the other conversation\'s draft rather than carrying text across', async () => {
    await mount('s1');
    type('question for s1');
    fixture.componentRef.setInput('draftKey', 's2');
    fixture.detectChanges();
    type('question for s2');

    fixture.componentRef.setInput('draftKey', 's1');
    fixture.detectChanges();

    expect(component.userInput()).toBe('question for s1');
  });

  it('forgets the new-conversation draft when the first send tears the composer down', async () => {
    await mount(NEW_CONVERSATION_DRAFT_KEY);
    type('first message');

    // The empty state's composer is replaced by the compact one on the first
    // send, so it is destroyed before another change detection pass can run
    // its draft effect.
    component.submitChatRequest();
    fixture.destroy();

    await mount(NEW_CONVERSATION_DRAFT_KEY);
    expect(component.userInput()).toBe('');
  });

  it('forgets the draft once the message is sent', async () => {
    await mount('s1');
    type('about to send');

    component.submitChatRequest();
    fixture.detectChanges();

    await mount('s1');
    expect(component.userInput()).toBe('');
  });

  it('brings an undelivered follow-up back as composer text, not as a chip', async () => {
    await mount('s1');
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.detectChanges();
    type('a follow-up');

    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true }));
    fixture.detectChanges();
    expect(component.queuedMessages().length).toBe(1);

    await mount('s1');

    // A chip would promise delivery at the end of a turn this component never
    // saw start, so the flush edge never comes and it would sit there forever.
    expect(component.queuedMessages()).toEqual([]);
    expect(component.userInput()).toBe('a follow-up');
  });

  it('puts queued follow-ups ahead of the live text, in the order they were typed', async () => {
    await mount('s1');
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.detectChanges();

    type('first');
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true }));
    fixture.detectChanges();
    type('second');
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true }));
    fixture.detectChanges();
    type('still being typed');

    await mount('s1');

    expect(component.userInput()).toBe('first\n\nsecond\n\nstill being typed');
  });

  it('does not bring back a follow-up the backend confirmed it holds', async () => {
    await mount('s1');
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.detectChanges();
    type('already delivered');

    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true }));
    fixture.detectChanges();
    // The arm lands: the backend is holding this text against the running turn,
    // so restoring it would be the duplicate.
    component.queuedMessages.update((queue) => queue.map((entry) => ({ ...entry, armed: true })));
    fixture.detectChanges();

    await mount('s1');

    expect(component.userInput()).toBe('');
  });

  it('keeps the new-conversation composer separate from any session', async () => {
    await mount(NEW_CONVERSATION_DRAFT_KEY);
    type('a thought, before I pick a thread');

    await mount('s1');
    expect(component.userInput()).toBe('');

    await mount(NEW_CONVERSATION_DRAFT_KEY);
    expect(component.userInput()).toBe('a thought, before I pick a thread');
  });

  it('remembers nothing for a placement that opts out', async () => {
    await mount(null);
    type('throwaway preview text');

    expect(localStorage.length).toBe(0);
  });

  // -- attachments ---------------------------------------------------------

  it('brings back the files attached to a conversation', async () => {
    stubReadyUploads = [readyUpload('u1'), readyUpload('u2')];
    await mount('s1');
    type('have a look at these');

    stubReadyUploads = [];
    stubSessionFiles = [serverFile('u1', 's1'), serverFile('u2', 's1')];
    await mount('s1');
    await settleMicrotasks();

    expect(component.restoredAttachments().map((a) => a.uploadId)).toEqual(['u1', 'u2']);
    expect(component.attachmentIds()).toEqual(['u1', 'u2']);
  });

  it('sends the restored ids rather than showing a card that does not travel', async () => {
    stubReadyUploads = [readyUpload('u1')];
    await mount('s1');
    type('about the attached file');

    stubReadyUploads = [];
    stubSessionFiles = [serverFile('u1', 's1')];
    await mount('s1');
    await settleMicrotasks();

    const sent: { fileUploadIds?: string[] }[] = [];
    component.messageSubmitted.subscribe((m) => sent.push(m));
    component.submitChatRequest();

    expect(sent[0].fileUploadIds).toEqual(['u1']);
    expect(component.restoredAttachments()).toEqual([]);
  });

  it('drops a restored attachment the server no longer confirms', async () => {
    stubReadyUploads = [readyUpload('u1'), readyUpload('u2')];
    await mount('s1');
    type('two files');

    // u2 was deleted from the file browser in the meantime. Held open so the
    // pre-reconcile frame is observable rather than a race with the mount.
    stubReadyUploads = [];
    let release!: (files: FileMetadata[]) => void;
    listSessionFilesImpl = () => new Promise((resolve) => (release = resolve));
    await mount('s1');

    // Painted straight from storage — the cards do not wait on the round trip.
    expect(component.restoredAttachments().map((a) => a.uploadId)).toEqual(['u1', 'u2']);

    release([serverFile('u1', 's1')]);
    await settleMicrotasks();

    expect(component.restoredAttachments().map((a) => a.uploadId)).toEqual(['u1']);
  });

  it('keeps the cached cards when the reconcile itself fails', async () => {
    stubReadyUploads = [readyUpload('u1')];
    await mount('s1');
    type('one file');

    stubReadyUploads = [];
    listSessionFilesImpl = async () => {
      throw new Error('network');
    };
    await mount('s1');
    await settleMicrotasks();

    // A blip must not silently strip an attachment; the send re-checks anyway.
    expect(component.restoredAttachments().map((a) => a.uploadId)).toEqual(['u1']);
  });

  it('lets a restored attachment be taken off the composer', async () => {
    stubReadyUploads = [readyUpload('u1')];
    await mount('s1');
    type('one file');

    stubReadyUploads = [];
    stubSessionFiles = [serverFile('u1', 's1')];
    await mount('s1');
    await settleMicrotasks();

    component.onFileRemove('u1');
    fixture.detectChanges();

    expect(component.attachmentIds()).toEqual([]);
    await mount('s1');
    await settleMicrotasks();
    expect(component.restoredAttachments()).toEqual([]);
  });

  it('does not carry one conversation\'s attachments into another', async () => {
    stubReadyUploads = [readyUpload('u1')];
    await mount('s1');
    type('for s1 only');

    fixture.componentRef.setInput('draftKey', 's2');
    fixture.componentRef.setInput('sessionId', 's2');
    fixture.detectChanges();

    expect(component.attachmentIds()).toEqual([]);
  });

  it('carries the attachments of a follow-up queued behind a streaming turn', async () => {
    stubReadyUploads = [readyUpload('u1')];
    await mount('s1');
    fixture.componentRef.setInput('isChatLoading', true);
    fixture.detectChanges();
    type('what does this say');
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true }));
    fixture.detectChanges();

    stubReadyUploads = [];
    stubSessionFiles = [serverFile('u1', 's1')];
    await mount('s1');
    await settleMicrotasks();

    // The question comes back with the file it was about, not without it.
    expect(component.userInput()).toBe('what does this say');
    expect(component.attachmentIds()).toEqual(['u1']);
  });

  // -- mentions -------------------------------------------------------------

  it('re-binds a restored `@`-mention to the current Agent row', async () => {
    await mount('s1');
    type('@Al');
    component.onMentionPicked(AGENTS[0]);
    fixture.detectChanges();

    await mount('s1');

    expect(component.mentionedAgent()?.agentId).toBe('a1');
    const sent: { mentionAgentId?: string }[] = [];
    component.messageSubmitted.subscribe((m) => sent.push(m));
    component.submitChatRequest();
    expect(sent[0].mentionAgentId).toBe('a1');
  });

  it('does not re-bind an Agent that is no longer offered', async () => {
    await mount('s1');
    type('@Al');
    component.onMentionPicked({ agentId: 'gone', name: 'Deleted', group: 'own' });
    fixture.detectChanges();

    await mount('s1');

    // The id resolves against a live list, so an Agent that was deleted or
    // unshared comes back as prose rather than as a binding the send rejects.
    expect(component.mentionedAgent()).toBeNull();
  });

  it('drops a pending `@`-mention when the conversation changes under it', async () => {
    await mount('s1');
    type('@Al');
    component.onMentionPicked(AGENTS[0]);
    fixture.detectChanges();
    expect(component.mentionedAgent()).not.toBeNull();

    fixture.componentRef.setInput('draftKey', 's2');
    fixture.detectChanges();

    expect(component.mentionedAgent()).toBeNull();
  });
});


describe('ChatInputComponent — compact layout, touch and first-send handoff', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;
  let submitted: Array<{ content: string }>;
  let handoff: ComposerHandoffService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
          },
        },
        { provide: ToastService, useValue: { error: () => undefined, warning: () => undefined, info: () => undefined } },
        { provide: ToolService, useValue: {} },
        {
          provide: VoiceChatService,
          useValue: { status: signal('idle'), isVoiceActive: signal(false), agentTranscript: signal('') },
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

    handoff = TestBed.inject(ComposerHandoffService);
    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    submitted = [];
    component.messageSubmitted.subscribe((m) => submitted.push(m));
  });

  function render(compact: boolean): void {
    fixture.componentRef.setInput('compact', compact);
    fixture.detectChanges();
    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  }

  function type(value: string): void {
    textarea.value = value;
    textarea.setSelectionRange(value.length, value.length);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function pressEnter(): KeyboardEvent {
    const event = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true });
    textarea.dispatchEvent(event);
    fixture.detectChanges();
    return event;
  }

  const row = () => fixture.nativeElement.querySelector('.composer-row') as HTMLElement;

  it('is one row in compact, and stacked in the empty state', () => {
    render(true);
    expect(row().classList).not.toContain('composer-row--stacked');

    fixture.componentRef.setInput('compact', false);
    fixture.detectChanges();
    expect(row().classList).toContain('composer-row--stacked');
    expect(row().classList).toContain('composer-row--full');
  });

  it('unfolds a compact draft once it wraps, and folds back only when it is empty', async () => {
    render(true);
    type('first line\nsecond line');
    expect(component['unfolded']()).toBe(true);
    await fixture.whenStable();
    expect(row().classList).toContain('composer-row--stacked');

    // Shortening it back onto one line does not fold: that would flicker at the wrap point.
    type('first line');
    expect(component['unfolded']()).toBe(true);

    type('');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(component['unfolded']()).toBe(false);
  });

  it('never unfolds in the empty state, which is stacked already', () => {
    render(false);
    type('first line\nsecond line');
    expect(component['unfolded']()).toBe(false);
  });

  it('keeps the model picker in the bar in the empty state and moves it under the shell in compact', () => {
    render(false);
    expect(fixture.nativeElement.querySelector('.composer-tools app-model-dropdown')).not.toBeNull();

    fixture.componentRef.setInput('compact', true);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.composer-tools app-model-dropdown')).toBeNull();
    expect(fixture.nativeElement.querySelector('app-model-dropdown[size="compact"]')).not.toBeNull();
  });

  it('on touch, return adds a line and a Send button appears once there is text', () => {
    render(true);
    component['isCoarsePointer'].set(true);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('button[aria-label="Send message"]')).toBeNull();

    type('hello');
    const enter = pressEnter();
    expect(enter.defaultPrevented).toBe(false);
    expect(submitted).toEqual([]);

    (fixture.nativeElement.querySelector('button[aria-label="Send message"]') as HTMLButtonElement).click();
    expect(submitted.map((m) => m.content)).toEqual(['hello']);
  });

  it('the empty-state composer leaves its height for the compact one on send', () => {
    render(false);
    const leave = vi.spyOn(handoff, 'leave');
    type('first question');
    pressEnter();
    expect(leave).toHaveBeenCalledTimes(1);
  });

  it('a compact composer does not leave a handoff when it sends', () => {
    render(true);
    const leave = vi.spyOn(handoff, 'leave');
    type('follow-up');
    pressEnter();
    expect(leave).not.toHaveBeenCalled();
  });
});

describe('ComposerHandoffService', () => {
  it('hands a height over once, and only while it is fresh', () => {
    vi.useFakeTimers();
    try {
      const service = new ComposerHandoffService();
      service.leave(132);
      expect(service.take()).toBe(132);
      expect(service.take()).toBeNull();

      service.leave(132);
      vi.advanceTimersByTime(2500);
      expect(service.take()).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('spliceDictation', () => {
  it('pads with a space only where the neighbours lack whitespace', () => {
    expect(spliceDictation({ before: 'Hi', after: 'there' }, 'you')).toEqual({
      value: 'Hi you there',
      caret: 6,
    });
    expect(spliceDictation({ before: 'Hi ', after: ' there' }, 'you')).toEqual({
      value: 'Hi you there',
      caret: 6,
    });
    expect(spliceDictation({ before: '', after: '' }, '  Hello.  ')).toEqual({
      value: 'Hello.',
      caret: 6,
    });
  });

  it('leaves the text alone when nothing was heard', () => {
    expect(spliceDictation({ before: 'a', after: 'b' }, '   ')).toEqual({ value: 'ab', caret: 1 });
  });
});

/** DictationService as a DI stand-in: the test drives status, transcript and the end. */
class DictationServiceStub {
  readonly status = signal<DictationStatus>('idle');
  readonly levels = signal<number[]>([0, 0.5, 1]);
  readonly unavailable = signal(false);
  readonly isSupported = signal(true);
  readonly transcript = signal('');
  readonly isActive = computed(() => this.status() !== 'idle');
  handlers: DictationHandlers | null = null;
  startError: Error | null = null;

  readonly start = vi.fn(async (handlers: DictationHandlers) => {
    if (this.startError) throw this.startError;
    this.handlers = handlers;
    this.status.set('listening');
  });
  readonly finish = vi.fn(() => this.status.set('finishing'));
  readonly cancel = vi.fn(() => {
    this.status.set('idle');
    this.transcript.set('');
  });

  /** The server's `done`: what DictationService does when Done completes. */
  end(text: string, reason: DictationEndReason = 'stopped'): void {
    const handlers = this.handlers;
    this.handlers = null;
    this.status.set('idle');
    this.transcript.set('');
    handlers?.onEnd(text, reason);
  }
}

describe('ChatInputComponent — dictation', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;
  let dictation: DictationServiceStub;
  let toast: { error: ReturnType<typeof vi.fn>; warning: ReturnType<typeof vi.fn>; info: ReturnType<typeof vi.fn> };
  let voiceActive: ReturnType<typeof signal<boolean>>;

  beforeEach(async () => {
    dictation = new DictationServiceStub();
    toast = { error: vi.fn(), warning: vi.fn(), info: vi.fn() };
    voiceActive = signal(false);
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: signal([]),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
          },
        },
        { provide: ToastService, useValue: toast },
        { provide: ToolService, useValue: {} },
        {
          provide: VoiceChatService,
          useValue: { status: signal('idle'), isVoiceActive: voiceActive, agentTranscript: signal('') },
        },
        { provide: DictationService, useValue: dictation },
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
    fixture.componentRef.setInput('autoFocus', false);
    fixture.detectChanges();
    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  });

  function type(value: string, caret = value.length): void {
    textarea.value = value;
    textarea.dispatchEvent(new Event('input'));
    textarea.setSelectionRange(caret, caret);
    fixture.detectChanges();
  }

  function button(label: string): HTMLButtonElement | null {
    return fixture.nativeElement.querySelector(`button[aria-label="${label}"]`);
  }

  async function startDictating(): Promise<void> {
    button('Dictate')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function hear(text: string): void {
    dictation.transcript.set(text);
    fixture.detectChanges();
  }

  it('offers Dictate only where the browser can record and the server allows it', () => {
    expect(button('Dictate')).not.toBeNull();

    dictation.unavailable.set(true);
    fixture.detectChanges();
    expect(button('Dictate')).toBeNull();

    dictation.unavailable.set(false);
    fixture.componentRef.setInput('showDictationControl', false);
    fixture.detectChanges();
    expect(button('Dictate')).toBeNull();
  });

  it('is disabled while voice mode is live', () => {
    voiceActive.set(true);
    fixture.detectChanges();
    expect(button('Dictate')!.disabled).toBe(true);
  });

  it('previews the transcript at the caret, read-only, without touching the typed text', async () => {
    type('Please  tomorrow', 7);
    await startDictating();

    expect(dictation.start).toHaveBeenCalledTimes(1);
    expect(textarea.readOnly).toBe(true);
    expect(textarea.classList).toContain('italic');
    expect(button('Cancel dictation')).not.toBeNull();
    expect(button('Insert dictated text')).not.toBeNull();

    hear('email the dean');
    expect(textarea.value).toBe('Please email the dean tomorrow');
    expect(component.userInput()).toBe('Please  tomorrow');
  });

  it('Done inserts the final text at the anchor and puts the caret after it', async () => {
    type('Hi');
    await startDictating();
    hear('there');

    button('Insert dictated text')!.click();
    expect(dictation.finish).toHaveBeenCalledTimes(1);

    dictation.end('there friend.');
    fixture.detectChanges();

    expect(component.userInput()).toBe('Hi there friend.');
    expect(textarea.value).toBe('Hi there friend.');
    expect(textarea.readOnly).toBe(false);
    expect(textarea.selectionStart).toBe('Hi there friend.'.length);
    expect(button('Dictate')).not.toBeNull();
  });

  it('Cancel restores the composer exactly', async () => {
    type('Keep me');
    await startDictating();
    hear('throw this away');

    button('Cancel dictation')!.click();
    fixture.detectChanges();

    expect(dictation.cancel).toHaveBeenCalledTimes(1);
    expect(component.userInput()).toBe('Keep me');
    expect(textarea.value).toBe('Keep me');
    expect(textarea.readOnly).toBe(false);
  });

  it('Enter finishes and Escape cancels, and neither sends', async () => {
    const sent: unknown[] = [];
    component.messageSubmitted.subscribe(message => sent.push(message));
    type('Draft');
    await startDictating();

    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
    expect(dictation.finish).toHaveBeenCalledTimes(1);
    dictation.end('more words');
    fixture.detectChanges();

    await startDictating();
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }));
    expect(dictation.cancel).toHaveBeenCalledTimes(1);

    expect(sent).toEqual([]);
    expect(component.userInput()).toBe('Draft more words');
  });

  it('an empty dictation leaves the text as it was', async () => {
    type('Unchanged');
    await startDictating();
    dictation.end('   ');
    fixture.detectChanges();
    expect(component.userInput()).toBe('Unchanged');
    expect(textarea.value).toBe('Unchanged');
  });

  it('says so when the environment has dictation switched off', async () => {
    dictation.startError = new DictationUnavailableError();
    type('Still here');
    await startDictating();

    expect(toast.info).toHaveBeenCalledWith('Dictation', 'Dictation is not available here.');
    expect(textarea.readOnly).toBe(false);
    expect(textarea.value).toBe('Still here');
  });

  it('a failure mid-dictation surfaces the message and restores the composer', async () => {
    type('Before');
    await startDictating();
    hear('lost words');

    const handlers = dictation.handlers!;
    dictation.status.set('idle');
    handlers.onError('Dictation was interrupted.');
    fixture.detectChanges();

    expect(toast.error).toHaveBeenCalledWith('Dictation', 'Dictation was interrupted.');
    expect(textarea.value).toBe('Before');
    expect(component.userInput()).toBe('Before');
  });

  it('a time-limit end still inserts, and explains why it stopped', async () => {
    await startDictating();
    dictation.end('long speech', 'limit');
    fixture.detectChanges();
    expect(component.userInput()).toBe('long speech');
    expect(toast.info).toHaveBeenCalledWith('Dictation', 'Dictation stopped at its time limit.');
  });
});
