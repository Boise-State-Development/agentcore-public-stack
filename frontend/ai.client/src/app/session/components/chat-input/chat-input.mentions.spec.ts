/**
 * Several `@`-mentions in one message (workflow runs, docs/specs/workflow-runs.md §20.1, §20.5).
 *
 * With `features.workflowRuns` and `features.handoffs` on, and the page offering it, the composer
 * keeps up to 8 mentions and sends them as `mentionAgentIds`; a ninth is refused with a helper
 * line. With either flag off it is the one-mention composer it always was.
 */
import { NO_ERRORS_SCHEMA, computed, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';
import { AgentMentionService, MentionableAgent } from '../../../agents/services/agent-mention.service';
import { SkillCommand, SkillCommandService } from '../../../services/skill/skill-command.service';
import { FileUploadService } from '../../../services/file-upload';
import { FEATURES } from '../../../services/features';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolService } from '../../../services/tool/tool.service';
import { ModelService } from '../../services/model/model.service';
import { VoiceChatService } from '../../services/voice';
import { SteeringService } from '../../services/chat/steering.service';
import { ComposerDraftStorageService } from '../../services/session/composer-draft-storage.service';
import { ChatInputComponent } from './chat-input.component';

const AGENTS: MentionableAgent[] = Array.from({ length: 10 }, (_, i) => ({
  agentId: `a${i + 1}`,
  name: `Agent${String.fromCharCode(65 + i)}`,
  group: 'own' as const,
}));

class SteeringServiceStub {
  readonly applied = signal<string[]>([]);
  canSteer(): boolean { return false; }
  markToolUsed(): void {}
  startTurn(): void {}
  async arm(): Promise<boolean> { return false; }
  async withdraw(): Promise<void> {}
  shouldHoldQueue(): boolean { return false; }
  publishQueue(): void {}
  carriedFor(): { id: string; text: string }[] { return []; }
  recordApplied(): void {}
  consumeApplied(): void {}
  reset(): void {}
}
class SkillCommandServiceStub {
  readonly commands = signal<SkillCommand[]>([]);
  readonly loading = signal(false);
  readonly agentLocked = signal(false);
  readonly resolving = signal(false);
  async load(): Promise<void> {}
  search(): SkillCommand[] { return []; }
  bySlugOrUndefined(): SkillCommand | undefined { return undefined; }
}
class ModelServiceStub {
  readonly selectedModel = signal({ modelId: 'm', inputModalities: ['TEXT'] });
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

interface Sent {
  content: string;
  mentionAgentId?: string;
  mentionAgentIds?: string[];
}

describe('ChatInputComponent — several `@`-mentions (workflow runs)', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let textarea: HTMLTextAreaElement;
  let sent: Sent[];

  async function build(options: { workflowRuns?: boolean; handoffs?: boolean; offer?: boolean; project?: boolean } = {}): Promise<void> {
    const { workflowRuns = true, handoffs = true, offer = true, project = false } = options;
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
            presigningCount: signal(0),
            readyUploadIds: signal([]),
            readyUploads: computed(() => []),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
            registerKnownFile: () => undefined,
          },
        },
        { provide: FEATURES, useValue: { workflowRuns, handoffs } },
        { provide: ToastService, useValue: { error: () => undefined, warning: () => undefined, info: () => undefined } },
        { provide: ToolService, useValue: {} },
        { provide: ModelService, useClass: ModelServiceStub },
        { provide: VoiceChatService, useValue: { status: signal('idle'), isVoiceActive: signal(false), agentTranscript: signal('') } },
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
    fixture.componentRef.setInput('sessionId', 'sess-1');
    fixture.componentRef.setInput('draftKey', 'sess-1');
    fixture.componentRef.setInput('offerMultipleMentions', offer);
    if (project) fixture.componentRef.setInput('project', { projectId: 'p1', name: 'P' });
    sent = [];
    component.messageSubmitted.subscribe((m) => sent.push(m as Sent));
    fixture.detectChanges();
    textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
  }

  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  function type(value: string): void {
    textarea.value = value;
    textarea.setSelectionRange(value.length, value.length);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  /** Type `@` at the end of the text and pick `agent`, as the menu would. */
  function mention(agent: MentionableAgent): void {
    type(`${component.userInput()}@`);
    component.onMentionPicked(agent);
    fixture.detectChanges();
  }

  it('keeps several mentions and sends every one, in order', async () => {
    await build();
    mention(AGENTS[0]);
    mention(AGENTS[1]);
    mention(AGENTS[2]);
    type(`${component.userInput()}review this`);

    expect(component.mentionedAgents().map((a) => a.agentId)).toEqual(['a1', 'a2', 'a3']);
    component.submitChatRequest();
    expect(sent[0].mentionAgentId).toBe('a1');
    expect(sent[0].mentionAgentIds).toEqual(['a1', 'a2', 'a3']);
    expect(component.mentionedAgents()).toEqual([]);
  });

  it('sends no mentionAgentIds for a single mention', async () => {
    await build();
    mention(AGENTS[0]);
    type(`${component.userInput()}hi`);
    component.submitChatRequest();
    expect(sent[0].mentionAgentId).toBe('a1');
    expect(sent[0].mentionAgentIds).toBeUndefined();
  });

  it('does not offer an Agent already mentioned', async () => {
    await build();
    mention(AGENTS[0]);
    type(`${component.userInput()}@Agent`);
    expect(component.isMentionMenuOpen()).toBe(true);
    expect(component.mentionResults().map((a) => a.agentId)).not.toContain('a1');
  });

  it('refuses a ninth with the helper line instead of the menu', async () => {
    await build();
    for (const agent of AGENTS.slice(0, 8)) mention(agent);
    type(`${component.userInput()}@`);

    expect(component.isMentionMenuOpen()).toBe(false);
    expect(component.mentionLimitReached()).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('Up to 8 Agents in one handoff');
  });

  it('drops a mention whose @Name is deleted, and promotes the next to first', async () => {
    await build();
    mention(AGENTS[0]);
    mention(AGENTS[1]);
    type(component.userInput().replace('@AgentA ', ''));

    expect(component.mentionedAgent()?.agentId).toBe('a2');
    expect(component.mentionedAgents().map((a) => a.agentId)).toEqual(['a2']);
  });

  it('does not reopen the menu when the caret is back inside a committed @Name', async () => {
    await build();
    mention(AGENTS[0]);
    textarea.setSelectionRange(4, 4); // inside "@Age|ntA"
    textarea.dispatchEvent(new Event('click'));
    fixture.detectChanges();
    expect(component.isMentionMenuOpen()).toBe(false);
  });

  it('restores every mention from a draft', async () => {
    await build();
    mention(AGENTS[0]);
    mention(AGENTS[1]);
    type(`${component.userInput()}draft text`);
    const stored = TestBed.inject(ComposerDraftStorageService).read('sess-1');
    expect(stored.mentionAgentId).toBe('a1');
    expect(stored.additionalMentionAgentIds).toEqual(['a2']);

    TestBed.resetTestingModule();
    await build();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(component.mentionedAgents().map((a) => a.agentId)).toEqual(['a1', 'a2']);
  });

  describe('off, the composer keeps one mention per turn', () => {
    for (const [label, options] of [
      ['features.workflowRuns off', { workflowRuns: false }],
      ['features.handoffs off', { handoffs: false }],
      ['the page not offering it', { offer: false }],
    ] as const) {
      it(label, async () => {
        await build(options);
        mention(AGENTS[0]);
        type(`${component.userInput()}@`);
        expect(component.isMentionMenuOpen()).toBe(false);
        expect(component.mentionLimitReached()).toBe(false);

        component.onMentionPicked(AGENTS[1]);
        expect(component.mentionedAgents().map((a) => a.agentId)).toEqual(['a1']);
        component.submitChatRequest();
        expect(sent[0].mentionAgentIds).toBeUndefined();
      });
    }

    it('in a project, where no mention is offered', async () => {
      await build({ project: true });
      type('@');
      expect(component.isMentionMenuOpen()).toBe(false);
    });
  });
});
