/**
 * Marks an input whose value Cmd/Ctrl+K and Enter hand to the search dialog
 * (the sidebar's conversation filter box).
 */
export const SEARCH_HANDOFF_ATTRIBUTE = 'data-search-handoff';

/** Cmd+K on macOS, Ctrl+K elsewhere; no other modifier, no key repeat. */
export function isSearchShortcut(event: KeyboardEvent): boolean {
  if (event.repeat || event.isComposing || event.altKey || event.shiftKey) return false;
  if (!(event.metaKey || event.ctrlKey)) return false;
  return event.key === 'k' || event.key === 'K';
}

/**
 * What the shortcut should open the dialog with for a keydown on `target`:
 * the field's value for the hand-off box, `''` anywhere else, and `null`
 * (leave the key alone) inside a rich-text editor.
 *
 * Plain inputs and textareas do not keep the key: the composer has focus on
 * every conversation page, so leaving it there would make the shortcut dead
 * exactly where people are, and nothing in the app binds Cmd/Ctrl+K in a
 * plain field. A `contenteditable` editor is where Cmd/Ctrl+K conventionally
 * means "insert link", so it keeps the key.
 */
export function searchShortcutHandoff(target: EventTarget | null): string | null {
  if (!(target instanceof Element)) return '';
  if (target instanceof HTMLElement && (target.isContentEditable || target.closest('[contenteditable]:not([contenteditable="false"])'))) {
    return null;
  }
  if (target instanceof HTMLInputElement && target.hasAttribute(SEARCH_HANDOFF_ATTRIBUTE)) return target.value;
  return '';
}
