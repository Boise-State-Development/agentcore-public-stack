import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ConversationModePickerComponent } from './conversation-mode-picker.component';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';

interface Prompt {
  prompt_id: string;
  name: string;
  description: string;
}

const GUIDED: Prompt = {
  prompt_id: 'b3e0384e',
  name: 'Guided Learning',
  description: 'Guides you to the answer through questions and hints.',
};
const CONCISE: Prompt = {
  prompt_id: 'p-2',
  name: 'Concise',
  description: 'Short answers.',
};

/** Stands in for the real service; the picker only reads four things and writes one. */
class FakeSystemPrompts {
  readonly prompts = signal<Prompt[]>([]);
  readonly activePromptId = signal<string | null>(null);
  readonly activePrompt = signal<Prompt | null>(null);
  readonly hasPrompts = signal(false);
  setActivePrompt = vi.fn().mockResolvedValue(undefined);
}

describe('ConversationModePickerComponent', () => {
  let service: FakeSystemPrompts;

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = new FakeSystemPrompts();
    TestBed.configureTestingModule({
      providers: [{ provide: SystemPromptsService, useValue: service }],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  function create(sessionId: string | null = 'sess-1') {
    const fixture = TestBed.createComponent(ConversationModePickerComponent);
    fixture.componentRef.setInput('sessionId', sessionId);
    fixture.detectChanges();
    return fixture;
  }

  function withPrompts(prompts: Prompt[]) {
    service.prompts.set(prompts);
    service.hasPrompts.set(prompts.length > 0);
  }

  it('renders nothing when no modes exist', () => {
    // The common case in every deployment until an admin authors one. A visible
    // control whose menu holds only "None" would be worse than no control.
    const fixture = create();
    expect(fixture.nativeElement.querySelector('button')).toBeNull();
  });

  it('renders a trigger once modes exist', () => {
    withPrompts([GUIDED]);
    const fixture = create();
    const trigger = fixture.nativeElement.querySelector('button') as HTMLButtonElement;
    expect(trigger).toBeTruthy();
    expect(trigger.textContent).toContain('Mode');
    expect(trigger.getAttribute('aria-label')).toBe('Set a conversation mode');
  });

  it('names the active mode in the trigger, so it is visible without opening', () => {
    withPrompts([GUIDED]);
    service.activePrompt.set(GUIDED);
    service.activePromptId.set(GUIDED.prompt_id);
    const fixture = create();
    const trigger = fixture.nativeElement.querySelector('button') as HTMLButtonElement;
    expect(trigger.textContent).toContain('Guided Learning');
    expect(trigger.getAttribute('aria-label')).toBe(
      'Conversation mode: Guided Learning. Change it.',
    );
  });

  it('accents the trigger only while a mode is on', () => {
    withPrompts([GUIDED]);
    const fixture = create();
    const off = fixture.componentInstance['triggerClass']();
    expect(off).not.toContain('bg-primary-50');

    service.activePrompt.set(GUIDED);
    fixture.detectChanges();
    expect(fixture.componentInstance['triggerClass']()).toContain('bg-primary-50');
  });

  it('persists a selection against the session', () => {
    withPrompts([GUIDED, CONCISE]);
    const fixture = create('sess-42');
    fixture.componentInstance['select'](CONCISE.prompt_id);
    expect(service.setActivePrompt).toHaveBeenCalledWith('sess-42', 'p-2');
  });

  it('offers None as the way back off a mode', () => {
    // The old passive chip had a dismiss X; that role lives in the menu now, so
    // losing the chip must not lose the ability to turn a mode off.
    withPrompts([GUIDED]);
    service.activePrompt.set(GUIDED);
    const fixture = create();
    fixture.componentInstance['select'](null);
    expect(service.setActivePrompt).toHaveBeenCalledWith('sess-1', null);
  });

  it('still selects before the first turn, when there is no session yet', () => {
    // `setActivePrompt` no-ops its write for a null session but keeps the choice
    // in memory, so the first message carries it.
    withPrompts([GUIDED]);
    const fixture = create(null);
    fixture.componentInstance['select'](GUIDED.prompt_id);
    expect(service.setActivePrompt).toHaveBeenCalledWith(null, 'b3e0384e');
  });

  it('does not reject when persistence fails', async () => {
    withPrompts([GUIDED]);
    service.setActivePrompt.mockRejectedValueOnce(new Error('offline'));
    const fixture = create();
    expect(() => fixture.componentInstance['select'](GUIDED.prompt_id)).not.toThrow();
    await Promise.resolve();
  });
});
