import { describe, it, expect } from 'vitest';
import { memoryEntry as entry } from '../../../testing/project-memory.fixtures';
import {
  changeCounts,
  compareItems,
  contributors,
  describeProvenance,
  estimateTokens,
  inlineRuns,
  itemProblem,
  linkParts,
  lintLine,
  parseItems,
  replacedLabel,
  aliasProblem,
  renderItems,
  resolveLink,
  slugProblem,
  splitAliases,
} from './memory-text';

describe('parseItems', () => {
  it('reads anchors, continuation lines and skips frontmatter', () => {
    const text = [
      '---',
      'name: sis',
      '---',
      '- Term codes are YYYYTT. <!-- e:aaaaaaaa -->',
      '- Two lines',
      '  of one fact. <!-- e:BBBBBBBB -->',
      '- New, with no anchor yet.',
    ].join('\n');
    expect(parseItems(text)).toEqual([
      { text: 'Term codes are YYYYTT.', anchor: 'aaaaaaaa' },
      { text: 'Two lines\nof one fact.', anchor: 'bbbbbbbb' },
      { text: 'New, with no anchor yet.', anchor: null },
    ]);
  });

  it('reads nothing from nothing', () => {
    expect(parseItems(null)).toEqual([]);
    expect(parseItems('\n\n')).toEqual([]);
  });
});

describe('compareItems', () => {
  it('marks kept, added and removed items on each side, with anchors hidden', () => {
    const diff = compareItems('- Kept. <!-- e:aaaaaaaa -->\n- Old. <!-- e:bbbbbbbb -->', '- Kept. <!-- e:aaaaaaaa -->\n- New.');
    expect(diff.current).toEqual([
      { kind: 'same', text: 'Kept.' },
      { kind: 'removed', text: 'Old.' },
    ]);
    expect(diff.proposed).toEqual([
      { kind: 'same', text: 'Kept.' },
      { kind: 'added', text: 'New.' },
    ]);
    expect(changeCounts(diff)).toEqual({ added: 1, removed: 1 });
  });

  it('reads a new file as all added', () => {
    expect(compareItems(null, '- A.\n- B.').proposed.map(i => i.kind)).toEqual(['added', 'added']);
  });
});

describe('links', () => {
  const entries = [entry('sis-conventions', { aliases: ['Term Codes'] }), entry('rate-limits')];

  it('resolve to a name or an alias, ignoring case, or to the index', () => {
    expect(resolveLink('SIS-Conventions', entries)).toBe('sis-conventions');
    expect(resolveLink('term codes', entries)).toBe('sis-conventions');
    expect(resolveLink('memory.md', entries)).toBe('MEMORY.md');
    expect(resolveLink('nowhere', entries)).toBeNull();
  });

  it('split text into runs and chips, keeping a dead link as one', () => {
    expect(linkParts('See [[rate-limits]] and [[ nowhere ]].', entries)).toEqual([
      { kind: 'text', text: 'See ' },
      { kind: 'link', name: 'rate-limits', slug: 'rate-limits' },
      { kind: 'text', text: ' and ' },
      { kind: 'link', name: 'nowhere', slug: null },
      { kind: 'text', text: '.' },
    ]);
    expect(linkParts('No links.', entries)).toEqual([{ kind: 'text', text: 'No links.' }]);
  });
});

describe('describeProvenance', () => {
  const people = { 'dana@x.edu': 'Dana Whitfield', 'pat@x.edu': 'Pat Editor' };

  it('names who added an item, and from a task when the assistant saved it', () => {
    expect(describeProvenance({ addedBy: 'dana@x.edu', addedAt: '2026-09-18T00:00:00Z' }, people, 'me@x.edu')).toEqual({
      text: 'Added by Dana Whitfield',
      at: '2026-09-18T00:00:00Z',
      sessionId: null,
      movedFrom: null,
    });
    const saved = describeProvenance(
      { addedBy: 'dana@x.edu', addedAt: 'a', sourceSessionId: 'sess-1' },
      people,
      'me@x.edu',
    );
    expect(saved.text).toBe('Saved by Dana Whitfield from a task');
    expect(saved.sessionId).toBeNull();
  });

  it('links the caller to their own task, and calls them you', () => {
    const mine = describeProvenance({ addedBy: 'Me@x.edu', addedAt: 'a', sourceSessionId: 'sess-1' }, people, 'me@x.edu');
    expect(mine).toEqual({ text: 'Saved by you from a task', at: 'a', sessionId: 'sess-1', movedFrom: null });
  });

  it('tells a proposal, an edit and a restore apart', () => {
    expect(
      describeProvenance({ addedBy: 'dana@x.edu', addedAt: 'a', proposalId: 'p1', proposedBy: 'dana@x.edu', approvedBy: 'pat@x.edu' }, people, null).text,
    ).toBe('Proposed by Dana Whitfield, approved by Pat Editor');
    expect(describeProvenance({ addedBy: 'dana@x.edu', addedAt: 'a', updatedBy: 'pat@x.edu', updatedAt: 'b' }, people, null)).toMatchObject({
      text: 'Added by Dana Whitfield, last edited by Pat Editor',
      at: 'b',
    });
    expect(describeProvenance({ addedBy: 'dana@x.edu', addedAt: 'a', restoredBy: 'pat@x.edu', restoredAt: 'c' }, people, null)).toMatchObject({
      text: 'Restored by Pat Editor',
      at: 'c',
    });
    expect(describeProvenance({ addedBy: 'nobody@x.edu', addedAt: 'a' }, people, null).text).toBe('Added by nobody@x.edu');
  });

  it('says so when an item predates provenance', () => {
    expect(describeProvenance(null, people, null).text).toBe('Added before item history was kept');
  });

  it('keeps who added a moved item, and says where it came from (2.6c)', () => {
    const moved = describeProvenance(
      { addedBy: 'dana@x.edu', addedAt: 'a', movedFrom: 'canvas', movedBy: 'pat@x.edu', movedAt: 'm' }, people, null,
    );
    expect(moved).toEqual({ text: 'Added by Dana Whitfield', at: 'a', sessionId: null, movedFrom: 'canvas' });
  });
});

describe('replacedLabel (2.6c)', () => {
  const row = (reason: 'merged' | 'superseded') => ({ archiveId: 'x', text: 't', reason, archivedAt: 'a', restorableUntil: 'b' });

  it('counts the items it shows: what merged in, and what it superseded', () => {
    expect(replacedLabel([row('merged')])).toBe('Merged with 1 other item');
    expect(replacedLabel([row('merged'), row('merged')])).toBe('Merged with 2 other items');
    expect(replacedLabel([row('superseded')])).toBe('Replaces an older item');
    expect(replacedLabel([row('superseded'), row('superseded')])).toBe('Replaces 2 older items');
    expect(replacedLabel([row('merged'), row('superseded')])).toBe('Merged with 1 other item · Replaces an older item');
  });
});

describe('contributors and estimateTokens', () => {
  it('collects everyone once', () => {
    expect(
      contributors([
        { addedBy: 'a@x.edu', addedAt: '', updatedBy: 'b@x.edu' },
        null,
        { addedBy: 'a@x.edu', addedAt: '', approvedBy: 'c@x.edu' },
      ]),
    ).toEqual(['a@x.edu', 'b@x.edu', 'c@x.edu']);
  });

  it('counts four characters a token, rounding up', () => {
    expect(estimateTokens('')).toBe(0);
    expect(estimateTokens('abcde')).toBe(2);
  });
});

describe('editor checks', () => {
  const entries = [entry('sis', { aliases: ['banner'] }), entry('rates')];

  it('renders items the way the backend does, so a proposal round-trips', () => {
    const text = renderItems([{ text: 'A.', anchor: 'aaaaaaaa' }, { text: 'Two\nlines.', anchor: null }]);
    expect(text).toBe('- A. <!-- e:aaaaaaaa -->\n- Two\n  lines.\n');
    expect(parseItems(text)).toEqual([{ text: 'A.', anchor: 'aaaaaaaa' }, { text: 'Two\nlines.', anchor: null }]);
  });

  it('checks a new name', () => {
    expect(slugProblem('', entries)).toBe('Give the file a name.');
    expect(slugProblem('MEMORY.md', entries)).toContain('is the index');
    expect(slugProblem('Has Spaces', entries)).toContain('Use lowercase letters');
    expect(slugProblem('BANNER'.toLowerCase(), entries)).toBe('A file already has that name or alias.');
    expect(slugProblem('people/jane-doe', entries)).toBeNull();
  });

  it('checks aliases against other files, never the file’s own', () => {
    expect(splitAliases(' banner , lms  sync,, ')).toEqual(['banner', 'lms sync']);
    expect(aliasProblem(['banner'], 'sis', entries)).toBeNull();
    expect(aliasProblem(['Banner'], 'rates', entries)).toBe('“Banner” already names “sis”.');
    expect(aliasProblem(['[x]'], 'rates', entries)).toContain('square brackets');
  });

  it('refuses anchors typed by hand and new dead links only', () => {
    expect(itemProblem('x <!-- e:aaaaaaaa -->', '', entries)).toContain('managed for you');
    expect(itemProblem('See [[nowhere]].', '', entries)).toBe('[[nowhere]] doesn’t match a file name or alias.');
    expect(itemProblem('See [[nowhere]] again.', 'See [[Nowhere]].', entries)).toBeNull();
    expect(itemProblem('See [[banner]] and [[memory.md]].', '', entries)).toBeNull();
  });
});

describe('inlineRuns', () => {
  it('reads bold, emphasis and code, and leaves everything else literal', () => {
    expect(inlineRuns('**Fixed price:** $184,500, *net* 30 per `§6`; 2 * 3 * 4')).toEqual([
      { kind: 'strong', text: 'Fixed price:' },
      { kind: 'plain', text: ' $184,500, ' },
      { kind: 'em', text: 'net' },
      { kind: 'plain', text: ' 30 per ' },
      { kind: 'code', text: '§6' },
      { kind: 'plain', text: '; 2 * 3 * 4' },
    ]);
    expect(inlineRuns('plain')).toEqual([{ kind: 'plain', text: 'plain' }]);
  });
});

describe('lintLine', () => {
  const finding = (message: string) => ({ rule: 'r', category: 'instruction' as const, where: 'item' as const, message, summary: message });

  it('is the first sentence, then how many more, or null for none', () => {
    expect(lintLine(undefined)).toBeNull();
    expect(lintLine([])).toBeNull();
    expect(lintLine([finding('Item 1 reads like an instruction.')])).toBe('Item 1 reads like an instruction.');
    expect(lintLine([finding('A.'), finding('B.'), finding('C.')])).toBe('A. (and 2 more)');
  });
});
