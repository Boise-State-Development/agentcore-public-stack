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
