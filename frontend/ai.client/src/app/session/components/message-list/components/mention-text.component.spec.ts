import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { AgentMentionService } from '../../../../agents/services/agent-mention.service';
import { SkillCommandService } from '../../../../services/skill/skill-command.service';
import {
  MentionTextComponent,
  splitMentions,
  splitSkillCommands,
} from './mention-text.component';

const NAMES = ['Brand Deck Builder', 'Brand', 'myBoiseState'];

describe('splitMentions', () => {
  it('returns the whole string when there is nothing to match', () => {
    expect(splitMentions('plain text', NAMES)).toEqual([{ text: 'plain text', isMention: false }]);
  });

  it('splits a multi-word agent name out of the surrounding prose', () => {
    expect(splitMentions('hey @Brand Deck Builder can you help', NAMES)).toEqual([
      { text: 'hey ', isMention: false },
      { text: '@Brand Deck Builder', isMention: true },
      { text: ' can you help', isMention: false },
    ]);
  });

  it('prefers the longest name so a prefix cannot swallow the match', () => {
    const segments = splitMentions('@Brand Deck Builder', NAMES);
    expect(segments).toEqual([{ text: '@Brand Deck Builder', isMention: true }]);
  });

  it('matches a mention at the start of the message', () => {
    expect(splitMentions('@myBoiseState what is my balance', NAMES)).toEqual([
      { text: '@myBoiseState', isMention: true },
      { text: ' what is my balance', isMention: false },
    ]);
  });

  it('matches case-insensitively', () => {
    expect(splitMentions('@mybOISEsTATE hi', NAMES)[0]).toEqual({
      text: '@mybOISEsTATE',
      isMention: true,
    });
  });

  it('leaves an email address alone — a mention has to start a word', () => {
    expect(splitMentions('mail me at phil@Brand.edu', NAMES)).toEqual([
      { text: 'mail me at phil@Brand.edu', isMention: false },
    ]);
  });

  it('does not match a name that only prefixes a longer word', () => {
    expect(splitMentions('@Branding is fun', NAMES)).toEqual([
      { text: '@Branding is fun', isMention: false },
    ]);
  });

  it('preserves newlines around a mention', () => {
    expect(splitMentions('line one\n@Brand\nline two', NAMES)).toEqual([
      { text: 'line one\n', isMention: false },
      { text: '@Brand', isMention: true },
      { text: '\nline two', isMention: false },
    ]);
  });

  it('handles regex metacharacters in an agent name', () => {
    expect(splitMentions('ask @C++ Helper about it', ['C++ Helper'])).toEqual([
      { text: 'ask ', isMention: false },
      { text: '@C++ Helper', isMention: true },
      { text: ' about it', isMention: false },
    ]);
  });

  it('renders plain when no names have loaded yet', () => {
    expect(splitMentions('@Brand hello', [])).toEqual([{ text: '@Brand hello', isMention: false }]);
  });
});

describe('MentionTextComponent', () => {
  class MentionServiceStub {
    readonly mentionable = signal([{ agentId: 'a1', name: 'Brand Deck Builder', group: 'own' }]);
    async load(): Promise<void> {}
  }

  async function render(text: string) {
    await TestBed.configureTestingModule({
      imports: [MentionTextComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(MentionTextComponent);
    fixture.componentRef.setInput('text', text);
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => TestBed.resetTestingModule());

  it('bolds the mention and leaves the rest of the text alone', async () => {
    const fixture = await render('hey @Brand Deck Builder look at line 2');
    const bold = fixture.nativeElement.querySelector('.font-semibold') as HTMLElement;
    expect(bold.textContent).toBe('@Brand Deck Builder');
  });

  it('reproduces the message text exactly — no stray whitespace around the runs', async () => {
    const source = 'hey @Brand Deck Builder\n  indented line';
    const fixture = await render(source);
    expect(fixture.nativeElement.textContent).toBe(source);
  });
});

/** Stand-in for SkillCommandService: the real one reaches SkillService → HttpClient. */
class SkillCommandServiceStub {
  readonly commands = signal([
    { skillId: 's1', slug: 'brand-deck', name: 'Brand Deck', description: '' },
  ]);
  async load(): Promise<void> {}
}

const SLUGS = ['brand-deck', 'web-research'];

/**
 * The second pass over what `splitMentions` left as prose. It runs separately because the
 * two are matched against different things — Agent names (with spaces) vs skill slugs
 * (without) — and because a mention must never be re-examined for slashes.
 */
describe('splitSkillCommands', () => {
  it('marks a known command', () => {
    expect(splitSkillCommands([{ text: 'run /brand-deck now', isMention: false }], SLUGS)).toEqual([
      { text: 'run ', isMention: false },
      { text: '/brand-deck', isMention: false, isCommand: true },
      { text: ' now', isMention: false },
    ]);
  });

  it('leaves an unknown slug as prose', () => {
    const input = [{ text: '/not-a-skill', isMention: false }];
    expect(splitSkillCommands(input, SLUGS)).toEqual(input);
  });

  it.each(['and/or', 'open 24/7', 'https://example.com', 'src/app/foo', '/brand-deck/x'])(
    'leaves %j as prose',
    (text) => {
      expect(splitSkillCommands([{ text, isMention: false }], SLUGS)).toEqual([
        { text, isMention: false },
      ]);
    },
  );

  it('never re-examines a mention run', () => {
    const input = [{ text: '@Brand/Deck', isMention: true }];
    expect(splitSkillCommands(input, SLUGS)).toEqual(input);
  });

  it('is inert with no known slugs', () => {
    const input = [{ text: '/brand-deck', isMention: false }];
    expect(splitSkillCommands(input, [])).toEqual(input);
  });

  it('reproduces the source text exactly across every run', () => {
    const source = 'hey /brand-deck and /web-research please';
    const joined = splitSkillCommands([{ text: source, isMention: false }], SLUGS)
      .map((segment) => segment.text)
      .join('');
    expect(joined).toBe(source);
  });
});
