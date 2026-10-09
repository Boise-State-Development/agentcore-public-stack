// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { isSearchShortcut, keepsSearchShortcut } from './search-shortcut';

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
    expect(keepsSearchShortcut(document.body)).toBe(false);
    expect(keepsSearchShortcut(document.createElement('button'))).toBe(false);
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    expect(keepsSearchShortcut(checkbox)).toBe(false);
    expect(keepsSearchShortcut(null)).toBe(false);
  });

  it('opens from plain fields such as the composer, without taking their text', () => {
    const composer = document.createElement('textarea');
    composer.value = 'half-typed prompt';
    expect(keepsSearchShortcut(composer)).toBe(false);
    expect(keepsSearchShortcut(document.createElement('input'))).toBe(false);
  });

  it('leaves rich-text editors alone (Cmd/Ctrl+K is their link key)', () => {
    const editable = document.createElement('div');
    editable.setAttribute('contenteditable', 'true');
    const inner = document.createElement('p');
    editable.appendChild(inner);
    document.body.appendChild(editable);
    try {
      expect(keepsSearchShortcut(editable)).toBe(true);
      expect(keepsSearchShortcut(inner)).toBe(true);
    } finally {
      editable.remove();
    }
  });
});
