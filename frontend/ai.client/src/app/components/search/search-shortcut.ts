/** Cmd+K on macOS, Ctrl+K elsewhere; no other modifier, no key repeat. */
export function isSearchShortcut(event: KeyboardEvent): boolean {
  if (event.repeat || event.isComposing || event.altKey || event.shiftKey) return false;
  if (!(event.metaKey || event.ctrlKey)) return false;
  return event.key === 'k' || event.key === 'K';
}

/**
 * Whether the element that has focus keeps Cmd/Ctrl+K for itself, so the
 * shortcut should leave the key alone.
 *
 * Plain inputs and textareas do not keep the key: the composer has focus on
 * every conversation page, so leaving it there would make the shortcut dead
 * exactly where people are, and nothing in the app binds Cmd/Ctrl+K in a
 * plain field. A `contenteditable` editor is where Cmd/Ctrl+K conventionally
 * means "insert link", so it keeps the key.
 */
export function keepsSearchShortcut(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.closest('[contenteditable]:not([contenteditable="false"])') !== null;
}

function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent);
}

/** The shortcut as this platform spells it, for a search button's tooltip. */
export function searchShortcutLabel(): string {
  return isApplePlatform() ? '⌘K' : 'Ctrl+K';
}

/** The shortcut as `aria-keyshortcuts` spells it. */
export function searchShortcutAria(): string {
  return isApplePlatform() ? 'Meta+K' : 'Control+K';
}
