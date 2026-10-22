/**
 * The app's global keyboard shortcuts: one table that the document handler
 * matches against, the buttons read their tooltip and `aria-keyshortcuts` from,
 * and the help sheet lists. A shortcut is defined once here, so the key a
 * tooltip advertises is always the key that works.
 *
 * Every entry carries a modifier. The composer has focus on every conversation
 * page, so a bare key (`/`, `?`) would type itself instead of acting.
 *
 * Browser-reserved chords are avoided on purpose: Cmd/Ctrl+N, T, W, L, D and
 * Shift+N/T/B are taken before the page sees them (or do something the user
 * expects from the browser), which is why "new conversation" is Shift+O.
 */

export type ShortcutId =
  | 'search'
  | 'newConversation'
  | 'focusComposer'
  | 'toggleSidebar'
  | 'copyLastResponse'
  | 'help';

export interface Shortcut {
  id: ShortcutId;
  /** What it does, as the help sheet lists it. */
  description: string;
  /** `KeyboardEvent.key`, compared case-insensitively. */
  key: string;
  /** Cmd on Apple platforms, Ctrl elsewhere. Either is accepted on both. */
  mod: boolean;
  /**
   * Whether Shift must be held. `'any'` for punctuation: on many layouts `/`
   * itself needs Shift (German is Shift+7), and the character the event
   * reports already says which key it was.
   */
  shift: boolean | 'any';
  /**
   * Leave the key to a focused rich-text editor. Cmd/Ctrl+K is the link key
   * in every one of them; plain inputs and textareas bind none of these.
   */
  yieldsToRichText?: boolean;
  /** The key as written on a keycap. */
  keyLabel: string;
  /** The key as `aria-keyshortcuts` spells it. */
  ariaKey: string;
}

export const SHORTCUTS: readonly Shortcut[] = [
  {
    id: 'search',
    description: 'Search conversations',
    key: 'k',
    mod: true,
    shift: false,
    yieldsToRichText: true,
    keyLabel: 'K',
    ariaKey: 'K',
  },
  {
    id: 'newConversation',
    description: 'New conversation',
    key: 'o',
    mod: true,
    shift: true,
    keyLabel: 'O',
    ariaKey: 'O',
  },
  {
    id: 'focusComposer',
    description: 'Focus the message box',
    key: 'Escape',
    mod: false,
    shift: true,
    keyLabel: 'Esc',
    ariaKey: 'Escape',
  },
  {
    id: 'toggleSidebar',
    description: 'Show or hide the sidebar',
    key: 's',
    mod: true,
    shift: true,
    keyLabel: 'S',
    ariaKey: 'S',
  },
  {
    id: 'copyLastResponse',
    description: 'Copy the last response',
    key: 'c',
    mod: true,
    shift: true,
    keyLabel: 'C',
    ariaKey: 'C',
  },
  {
    id: 'help',
    description: 'Show keyboard shortcuts',
    key: '/',
    mod: true,
    shift: 'any',
    keyLabel: '/',
    ariaKey: '/',
  },
];

export function shortcut(id: ShortcutId): Shortcut {
  const found = SHORTCUTS.find(s => s.id === id);
  if (!found) throw new Error(`Unknown shortcut: ${id}`);
  return found;
}

/** Whether `event` is `s`. Key repeat and IME composition never are. */
export function matchesShortcut(event: KeyboardEvent, s: Shortcut): boolean {
  if (event.repeat || event.isComposing || event.altKey) return false;
  if (event.key.toLowerCase() !== s.key.toLowerCase()) return false;
  const mod = event.metaKey || event.ctrlKey;
  if (mod !== s.mod) return false;
  return s.shift === 'any' || event.shiftKey === s.shift;
}

/** The shortcut `event` is, if any. */
export function shortcutFor(event: KeyboardEvent): Shortcut | null {
  return SHORTCUTS.find(s => matchesShortcut(event, s)) ?? null;
}

/**
 * Whether the focused element keeps `s` for itself. Only a `contenteditable`
 * editor does, and only for shortcuts that are conventional editor keys.
 *
 * Plain inputs and textareas never keep a shortcut: the composer has focus on
 * every conversation page, so yielding there would make every shortcut dead
 * exactly where people are.
 */
export function targetKeepsShortcut(target: EventTarget | null, s: Shortcut): boolean {
  if (!s.yieldsToRichText || !(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.closest('[contenteditable]:not([contenteditable="false"])') !== null;
}

export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent);
}

/** The keycaps to press, in order: `['⌘', '⇧', 'O']` or `['Ctrl', 'Shift', 'O']`. */
export function shortcutKeys(s: Shortcut, apple = isApplePlatform()): string[] {
  const keys: string[] = [];
  if (s.mod) keys.push(apple ? '⌘' : 'Ctrl');
  if (s.shift === true) keys.push(apple ? '⇧' : 'Shift');
  keys.push(s.keyLabel);
  return keys;
}

/** The shortcut as this platform writes it, for a tooltip: `⌘⇧O`, `Ctrl+Shift+O`. */
export function shortcutLabel(id: ShortcutId, apple = isApplePlatform()): string {
  return shortcutKeys(shortcut(id), apple).join(apple ? '' : '+');
}

/** The shortcut as `aria-keyshortcuts` spells it: `Meta+Shift+O`, `Control+Shift+O`. */
export function shortcutAria(id: ShortcutId, apple = isApplePlatform()): string {
  const s = shortcut(id);
  const parts: string[] = [];
  if (s.mod) parts.push(apple ? 'Meta' : 'Control');
  if (s.shift === true) parts.push('Shift');
  parts.push(s.ariaKey);
  return parts.join('+');
}

const SPOKEN: Record<string, string> = {
  '⌘': 'Command',
  '⇧': 'Shift',
  Ctrl: 'Control',
  Esc: 'Escape',
  '/': 'Slash',
};

/**
 * The keys as a screen reader should say them: `Command Shift O`. VoiceOver
 * reads `⇧` as "up white arrow", so the sheet speaks this instead of its caps.
 */
export function shortcutSpoken(s: Shortcut, apple = isApplePlatform()): string {
  return shortcutKeys(s, apple).map(k => SPOKEN[k] ?? k).join(' ');
}

/** `label` with the shortcut appended for a tooltip: `New chat (⌘⇧O)`. */
export function withShortcut(label: string, id: ShortcutId): string {
  return `${label} (${shortcutLabel(id)})`;
}
