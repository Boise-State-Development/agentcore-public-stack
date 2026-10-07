// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { isSearchShortcut, searchShortcutHandoff } from './search-shortcut';

const key = (init: KeyboardEventInit) => new KeyboardEvent('keydown', init);

describe('search shortcut', () => {
  it('is Cmd+K or Ctrl+K and nothing else', () => {
    expect(isSearchShortcut(key({ key: 'k', metaKey: true }))).toBe(true);
    expect(isSearchShortcut(key({ key: 'K', ctrlKey: true }))).toBe(true);
    expect(isSearchShortcut(key({ key: 'k' }))).toBe(false);
    expect(isSearchShortcut(key({ key: 'k', metaKey: true, shiftKey: true }))).toBe(false);
    expect(isSearchShortcut(key({ key: 'k', ctrlKey: true, altKey: true }))).toBe(false);
    expect(isSearchShortcut(key({ key: 'k', metaKey: true, repeat: true }))).toBe(false);
    expect(isSearchShortcut(key({ key: 'j', metaKey: true }))).toBe(false);
  });

  it('opens empty from anywhere that is not editable', () => {
    expect(searchShortcutHandoff(document.body)).toBe('');
    expect(searchShortcutHandoff(document.createElement('button'))).toBe('');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    expect(searchShortcutHandoff(checkbox)).toBe('');
    expect(searchShortcutHandoff(null)).toBe('');
  });

  it('opens from plain fields such as the composer, without taking their text', () => {
    const composer = document.createElement('textarea');
    composer.value = 'half-typed prompt';
    expect(searchShortcutHandoff(composer)).toBe('');
    expect(searchShortcutHandoff(document.createElement('input'))).toBe('');
  });

  it('leaves rich-text editors alone (Cmd/Ctrl+K is their link key)', () => {
    const editable = document.createElement('div');
    editable.setAttribute('contenteditable', 'true');
    const inner = document.createElement('p');
    editable.appendChild(inner);
    document.body.appendChild(editable);
    try {
      expect(searchShortcutHandoff(editable)).toBeNull();
      expect(searchShortcutHandoff(inner)).toBeNull();
    } finally {
      editable.remove();
    }
  });

  it('carries the sidebar filter box query over', () => {
    const box = document.createElement('input');
    box.setAttribute('data-search-handoff', '');
    box.value = 'syllabus';
    expect(searchShortcutHandoff(box)).toBe('syllabus');
  });
});
