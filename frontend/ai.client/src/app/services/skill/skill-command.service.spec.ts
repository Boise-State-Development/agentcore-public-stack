import { describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ConfigService } from '../config.service';
import { SkillService } from './skill.service';
import { SkillCommandService, findSkillCommands, removeSkillCommand } from './skill-command.service';

const SLUGS = ['brand-deck', 'web-research', 'usr'];

/**
 * The composer derives the skills a message invokes straight from its text, so these two
 * functions ARE the binding. Everything a `/` menu pick produces, a hand-typed command has
 * to produce identically — and everything a person writes with a slash in it has to stay
 * prose.
 */
describe('findSkillCommands', () => {
  it('finds a command at the start of the message', () => {
    expect(findSkillCommands('/brand-deck make me a deck', SLUGS)).toEqual(['brand-deck']);
  });

  it('finds a command mid-sentence', () => {
    expect(findSkillCommands('please /web-research this', SLUGS)).toEqual(['web-research']);
  });

  it('finds several, in the order they were written', () => {
    expect(findSkillCommands('/web-research then /brand-deck', SLUGS)).toEqual([
      'web-research',
      'brand-deck',
    ]);
  });

  it('de-duplicates a slug used twice', () => {
    expect(findSkillCommands('/brand-deck and /brand-deck', SLUGS)).toEqual(['brand-deck']);
  });

  it('matches across a newline', () => {
    expect(findSkillCommands('do this\n/brand-deck', SLUGS)).toEqual(['brand-deck']);
  });

  it('ignores a slug the user cannot invoke', () => {
    expect(findSkillCommands('/some-other-skill', SLUGS)).toEqual([]);
  });

  it.each([
    ['and/or are both fine', 'a slash inside a word'],
    ['open 24/7 all year', 'a fraction'],
    ['see https://example.com/x', 'a URL'],
    ['edit src/app/foo.ts', 'a relative path'],
  ])('leaves %j alone (%s)', (text) => {
    expect(findSkillCommands(text, SLUGS)).toEqual([]);
  });

  it('stops at the second slash of an absolute path', () => {
    // An absolute path starts a word exactly like a command does, and `usr` IS in the
    // slug list here on purpose — so the trailing-slash half of the token rule is the
    // only thing standing between `/usr/bin/env` and a silently invoked skill.
    expect(findSkillCommands('/usr/bin/env', SLUGS)).toEqual([]);
  });

  it('still matches a command followed by ordinary punctuation', () => {
    expect(findSkillCommands('use /brand-deck, then stop', SLUGS)).toEqual(['brand-deck']);
    expect(findSkillCommands('use /brand-deck.', SLUGS)).toEqual(['brand-deck']);
  });

  it('is inert with no known slugs', () => {
    expect(findSkillCommands('/brand-deck', [])).toEqual([]);
  });
});

describe('removeSkillCommand', () => {
  it('removes a leading command and the space it left behind', () => {
    expect(removeSkillCommand('/brand-deck make me a deck', 'brand-deck')).toBe(
      'make me a deck',
    );
  });

  it('removes a mid-sentence command without eating the words around it', () => {
    expect(removeSkillCommand('please /web-research this', 'web-research')).toBe(
      'please this',
    );
  });

  it('removes every occurrence, since the chip stands for all of them', () => {
    expect(removeSkillCommand('/brand-deck a /brand-deck b', 'brand-deck')).toBe('a b');
  });

  it('leaves the other commands in place', () => {
    expect(removeSkillCommand('/brand-deck /web-research go', 'brand-deck')).toBe(
      '/web-research go',
    );
  });

  it('leaves text with no such command untouched', () => {
    expect(removeSkillCommand('nothing to remove', 'brand-deck')).toBe('nothing to remove');
  });
});

describe('SkillCommandService under an Agent lock', () => {
  function setup(): { commands: SkillCommandService; skills: SkillService } {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    return { commands: TestBed.inject(SkillCommandService), skills: TestBed.inject(SkillService) };
  }

  it('offers the resolved entries as commands, with the server slug, without any /skills/ load', () => {
    const { commands, skills } = setup();
    // Nothing loaded from /skills/: the viewer's own list is empty, and the owner's
    // skill below would never be in it anyway.
    skills.lockToAgentSkills([
      { skillId: 'web_research', displayName: 'Web Research', description: 'Research the web', slug: 'web-research' },
      { skillId: 'owners_style', displayName: "Owner's Style", description: 'House style', slug: 'owners-style' },
    ]);
    expect(commands.agentLocked()).toBe(true);
    expect(commands.resolving()).toBe(false);
    expect(commands.commands().map((c) => [c.slug, c.name])).toEqual([
      ['owners-style', "Owner's Style"],
      ['web-research', 'Web Research'],
    ]);
    expect(commands.bySlugOrUndefined('owners-style')?.skillId).toBe('owners_style');
  });

  it('reports resolving, with no commands, while the id lock waits for the resolved list', () => {
    const { commands, skills } = setup();
    skills.lockToAgentSkillIds(['owners_style'], { resolving: true });
    expect(commands.agentLocked()).toBe(true);
    expect(commands.resolving()).toBe(true);
    expect(commands.commands()).toEqual([]);
  });
});
