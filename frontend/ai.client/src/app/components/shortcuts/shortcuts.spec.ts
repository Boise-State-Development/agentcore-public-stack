// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  SHORTCUTS,
  matchesShortcut,
  shortcut,
  shortcutAria,
  shortcutFor,
  shortcutKeys,
  shortcutLabel,
  shortcutSpoken,
  targetKeepsShortcut,
  withShortcut,
} from './shortcuts';

const key = (init: KeyboardEventInit) => new KeyboardEvent('keydown', init);

describe('the shortcut table', () => {
  it('has one entry per id', () => {
    const ids = SHORTCUTS.map(s => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('never binds a bare key: the composer has focus and would type it', () => {
    for (const s of SHORTCUTS) {
      expect(s.mod || s.shift === true, s.id).toBe(true);
    }
  });

  it('stays off the chords the browser keeps for itself', () => {
    const reserved = ['n', 't', 'w', 'l', 'd', 'q', 'r', 'p'];
    for (const s of SHORTCUTS.filter(s => s.mod)) {
      expect(reserved, s.id).not.toContain(s.key.toLowerCase());
    }
  });
});

describe('matching a key press', () => {
  it('is Cmd+K or Ctrl+K for search and nothing else', () => {
    const search = shortcut('search');
    expect(matchesShortcut(key({ key: 'k', metaKey: true }), search)).toBe(true);
    expect(matchesShortcut(key({ key: 'K', ctrlKey: true }), search)).toBe(true);
    expect(matchesShortcut(key({ key: 'k' }), search)).toBe(false);
    expect(matchesShortcut(key({ key: 'k', metaKey: true, shiftKey: true }), search)).toBe(false);
    expect(matchesShortcut(key({ key: 'k', ctrlKey: true, altKey: true }), search)).toBe(false);
    expect(matchesShortcut(key({ key: 'k', metaKey: true, repeat: true }), search)).toBe(false);
    expect(matchesShortcut(key({ key: 'j', metaKey: true }), search)).toBe(false);
  });

  it('finds each shortcut from its key press', () => {
    expect(shortcutFor(key({ key: 'O', metaKey: true, shiftKey: true }))?.id).toBe('newConversation');
    expect(shortcutFor(key({ key: 'o', ctrlKey: true, shiftKey: true }))?.id).toBe('newConversation');
    expect(shortcutFor(key({ key: 'Escape', shiftKey: true }))?.id).toBe('focusComposer');
    expect(shortcutFor(key({ key: 'S', metaKey: true, shiftKey: true }))?.id).toBe('toggleSidebar');
    expect(shortcutFor(key({ key: 'C', ctrlKey: true, shiftKey: true }))?.id).toBe('copyLastResponse');
    expect(shortcutFor(key({ key: '/', metaKey: true }))?.id).toBe('help');
  });

  it('leaves the unmodified and plain-modifier forms alone', () => {
    expect(shortcutFor(key({ key: 'Escape' }))).toBeNull();
    expect(shortcutFor(key({ key: 'c', metaKey: true }))).toBeNull();
    expect(shortcutFor(key({ key: 's', ctrlKey: true }))).toBeNull();
    expect(shortcutFor(key({ key: 'o', metaKey: true }))).toBeNull();
    expect(shortcutFor(key({ key: '/' }))).toBeNull();
    expect(shortcutFor(key({ key: 'Escape', shiftKey: true, metaKey: true }))).toBeNull();
  });

  it('takes Cmd/Ctrl+/ on layouts where / needs Shift', () => {
    expect(shortcutFor(key({ key: '/', ctrlKey: true, shiftKey: true }))?.id).toBe('help');
  });

  it('ignores a key that is still being composed by an IME', () => {
    expect(shortcutFor(key({ key: 'o', metaKey: true, shiftKey: true, isComposing: true }))).toBeNull();
  });
});

describe('where a shortcut yields', () => {
  it('fires from anywhere that is not a rich-text editor', () => {
    const search = shortcut('search');
    expect(targetKeepsShortcut(document.body, search)).toBe(false);
    expect(targetKeepsShortcut(document.createElement('button'), search)).toBe(false);
    expect(targetKeepsShortcut(null, search)).toBe(false);
  });

  it('fires from plain fields such as the composer', () => {
    const composer = document.createElement('textarea');
    composer.value = 'half-typed prompt';
    for (const s of SHORTCUTS) {
      expect(targetKeepsShortcut(composer, s), s.id).toBe(false);
    }
  });

  it('leaves Cmd/Ctrl+K to rich-text editors (their link key), and only that', () => {
    const editable = document.createElement('div');
    editable.setAttribute('contenteditable', 'true');
    const inner = document.createElement('p');
    editable.appendChild(inner);
    document.body.appendChild(editable);
    try {
      expect(targetKeepsShortcut(editable, shortcut('search'))).toBe(true);
      expect(targetKeepsShortcut(inner, shortcut('search'))).toBe(true);
      expect(targetKeepsShortcut(inner, shortcut('newConversation'))).toBe(false);
    } finally {
      editable.remove();
    }
  });
});

describe('how a shortcut is written', () => {
  it('uses symbols on Apple platforms and words elsewhere', () => {
    expect(shortcutLabel('newConversation', true)).toBe('⌘⇧O');
    expect(shortcutLabel('newConversation', false)).toBe('Ctrl+Shift+O');
    expect(shortcutLabel('focusComposer', true)).toBe('⇧Esc');
    expect(shortcutLabel('focusComposer', false)).toBe('Shift+Esc');
    expect(shortcutLabel('help', true)).toBe('⌘/');
    expect(shortcutLabel('search', false)).toBe('Ctrl+K');
  });

  it('spells aria-keyshortcuts the way the spec wants', () => {
    expect(shortcutAria('search', true)).toBe('Meta+K');
    expect(shortcutAria('search', false)).toBe('Control+K');
    expect(shortcutAria('copyLastResponse', false)).toBe('Control+Shift+C');
    expect(shortcutAria('focusComposer', true)).toBe('Shift+Escape');
  });

  it('gives the help sheet keycaps and words a screen reader can say', () => {
    expect(shortcutKeys(shortcut('toggleSidebar'), true)).toEqual(['⌘', '⇧', 'S']);
    expect(shortcutSpoken(shortcut('toggleSidebar'), true)).toBe('Command Shift S');
    expect(shortcutSpoken(shortcut('help'), false)).toBe('Control Slash');
    expect(shortcutSpoken(shortcut('focusComposer'), true)).toBe('Shift Escape');
  });

  it('appends the shortcut to a tooltip', () => {
    expect(withShortcut('New chat', 'newConversation')).toMatch(/^New chat \((⌘⇧O|Ctrl\+Shift\+O)\)$/);
  });
});
