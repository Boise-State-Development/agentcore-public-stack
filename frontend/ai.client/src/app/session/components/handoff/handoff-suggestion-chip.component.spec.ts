import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentMentionService, MentionableAgent } from '../../../agents/services/agent-mention.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffView } from '../../services/handoff/handoff.models';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { HandoffSuggestionChipComponent } from './handoff-suggestion-chip.component';

const CHIP: HandoffView = {
  handoffId: 's1',
  sessionId: 'src',
  kind: 'suggested',
  status: 'open',
  agentId: '',
  agentName: '',
  request: '',
  sourceMessageIndex: -1,
  excerptTokens: 0,
  createdAt: '2026-11-19T00:00:00Z',
  updatedAt: '2026-11-19T00:00:00Z',
  title: 'Draft the BIO 101 rubric',
  tldr: 'The rubric is its own deliverable.',
  agentHint: 'rubric builder',
  toolUseId: 'tu-1',
};

const AGENTS: MentionableAgent[] = [
  { agentId: 'rubric', name: 'Rubric Builder', group: 'own' },
  { agentId: 'docx', name: 'Docx Dogfood', group: 'pinned' },
];

describe('HandoffSuggestionChipComponent', () => {
  let fixture: ComponentFixture<HandoffSuggestionChipComponent>;
  const startSuggestion = vi.fn().mockResolvedValue(undefined);
  const dismissSuggestion = vi.fn().mockResolvedValue(undefined);
  const load = vi.fn().mockResolvedValue(undefined);

  function render(chip: HandoffView): HTMLElement {
    fixture = TestBed.createComponent(HandoffSuggestionChipComponent);
    fixture.componentRef.setInput('chip', chip);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const buttons = (el: HTMLElement) => [...el.querySelectorAll('button')];

  beforeEach(() => {
    startSuggestion.mockClear();
    dismissSuggestion.mockClear();
    TestBed.configureTestingModule({
      imports: [HandoffSuggestionChipComponent],
      providers: [
        { provide: AgentHandoffService, useValue: { startSuggestion, dismissSuggestion } },
        { provide: AgentMentionService, useValue: { mentionable: signal(AGENTS), load } },
      ],
    });
  });

  it('names the matched Agent on Start, and Start passes it', () => {
    const el = render(CHIP);

    expect(el.textContent).toContain('Draft the BIO 101 rubric');
    expect(el.textContent).toContain('The rubric is its own deliverable.');
    expect(el.querySelector('select')).toBeNull();
    const start = buttons(el).find((b) => b.textContent?.includes('Hand off to Rubric Builder'))!;
    start.click();

    expect(startSuggestion).toHaveBeenCalledWith(CHIP, { agentId: 'rubric', name: 'Rubric Builder' });
  });

  it('asks for an Agent when the model named one the user does not have', () => {
    const el = render({ ...CHIP, agentHint: 'Grant Writer' });

    const select = el.querySelector('select')!;
    const label = el.querySelector(`label[for="${select.id}"]`);
    expect(label?.textContent).toContain('“Grant Writer” isn’t one of yours');
    const start = buttons(el).find((b) => b.textContent?.trim() === 'Start')!;
    expect(start.disabled).toBe(true);

    select.value = 'docx';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(start.disabled).toBe(false);
    start.click();

    expect(startSuggestion).toHaveBeenCalledWith(expect.objectContaining({ handoffId: 's1' }), {
      agentId: 'docx',
      name: 'Docx Dogfood',
    });
  });

  it('asks for an Agent when the model named none', () => {
    const el = render({ ...CHIP, agentHint: undefined });

    expect(el.querySelector('label')?.textContent?.trim()).toBe('Pick an Agent');
  });

  it('Dismiss dismisses', () => {
    const el = render(CHIP);

    buttons(el).find((b) => b.textContent?.trim() === 'Dismiss')!.click();

    expect(dismissSuggestion).toHaveBeenCalledWith(CHIP);
  });

  it('says it is under review instead of offering Start while its sheet is open', () => {
    const state = TestBed.inject(HandoffStateService);
    state.setReview('src', {
      draft: { agentId: 'rubric', agentName: 'Rubric Builder', request: '', suggestionId: 's1' },
      phase: 'loading',
      request: '',
      card: '',
      cardRemoved: false,
      fileIds: [],
    });
    const el = render(CHIP);

    expect(el.querySelector('[role="status"]')?.textContent).toContain('Reviewing it below');
    expect(buttons(el)).toHaveLength(0);
  });

  it('is a section named by its title', () => {
    const el = render(CHIP);

    const section = el.querySelector('section')!;
    const heading = el.querySelector(`#${section.getAttribute('aria-labelledby')}`);
    expect(heading?.textContent).toBe('Draft the BIO 101 rubric');
  });
});
