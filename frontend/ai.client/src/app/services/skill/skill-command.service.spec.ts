import { describe, expect, it } from 'vitest';
import { findSkillCommands, removeSkillCommand } from './skill-command.service';

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
