import { Injectable, computed, inject, signal } from '@angular/core';
import { moveItemInArray } from '@angular/cdk/drag-drop';
import { FEATURES } from '../../services/features';
import { SidebarItemPreference, UserSettingsService } from '../../services/user-settings.service';
import {
  SIDEBAR_ITEMS,
  SidebarItemDef,
  defaultSidebarLayout,
  resolveSidebarLayout,
  sameSidebarLayout,
} from './sidebar-items';

/** An entry as the sidebar draws it: its definition plus where the user put it. */
export interface SidebarEntry extends SidebarItemDef {
  visible: boolean;
}

/**
 * First-paint copy of the saved layout. The account setting is the source of
 * truth; this only spares a returning user the sidebar redrawing itself when
 * `/users/me/settings` lands a moment after the first frame.
 */
const LAYOUT_CACHE_KEY = 'sidebar-layout';

/**
 * The user's sidebar layout: which navigation entries are shown, in what
 * order, and which wait under More.
 *
 * Edits apply to the sidebar at once, so the user sees each change behind the
 * Edit sidebar dialog; {@link save} persists them to the account's settings
 * when the dialog closes, rather than one request per checkbox or drag.
 */
@Injectable({ providedIn: 'root' })
export class SidebarLayoutService {
  private readonly userSettings = inject(UserSettingsService);
  private readonly features = inject(FEATURES);

  /** Every entry this build defines, including ones a feature switch hides. */
  private readonly layout = signal<SidebarItemPreference[]>(resolveSidebarLayout(readCachedLayout()));

  /** The layout as last read from or written to the account. */
  private persisted: SidebarItemPreference[] = this.layout();

  /** The entries this build offers, in the user's order. */
  readonly entries = computed<SidebarEntry[]>(() => {
    const defs = new Map(SIDEBAR_ITEMS.map(def => [def.id, def]));
    return this.layout()
      .map(pref => ({ ...defs.get(pref.id)!, visible: pref.visible }))
      .filter(entry => this.isAvailable(entry));
  });

  readonly visibleEntries = computed(() => this.entries().filter(entry => entry.visible));
  readonly hiddenEntries = computed(() => this.entries().filter(entry => !entry.visible));

  readonly isDefault = computed(() => sameSidebarLayout(this.layout(), defaultSidebarLayout()));

  constructor() {
    void this.load();
  }

  setVisible(id: string, visible: boolean): void {
    this.layout.update(layout => layout.map(pref => (pref.id === id ? { ...pref, visible } : pref)));
  }

  /**
   * Move an entry between two positions in {@link entries}. Entries a feature
   * switch hides keep their slots, so a build that shows them again finds
   * them where they were.
   */
  move(from: number, to: number): void {
    if (from === to) {
      return;
    }
    const layout = [...this.layout()];
    const available = new Set(this.entries().map(entry => entry.id));
    const slots = layout.flatMap((pref, index) => (available.has(pref.id) ? [index] : []));
    const shown = slots.map(slot => layout[slot]);
    moveItemInArray(shown, from, to);
    slots.forEach((slot, i) => (layout[slot] = shown[i]));
    this.layout.set(layout);
  }

  reset(): void {
    this.layout.set(defaultSidebarLayout());
  }

  /**
   * Persist the layout if it changed since it was last read or saved. The
   * default layout is saved as null, so a user who resets follows future
   * changes to the default rather than a frozen copy of today's.
   */
  async save(): Promise<void> {
    const layout = this.layout();
    if (sameSidebarLayout(layout, this.persisted)) {
      return;
    }
    writeCachedLayout(layout);
    await this.userSettings.updateSettings({ sidebarItems: this.isDefault() ? null : layout });
    this.persisted = layout;
  }

  private async load(): Promise<void> {
    let saved: SidebarItemPreference[] | null | undefined;
    try {
      saved = (await this.userSettings.getSettings()).sidebarItems;
    } catch {
      // The first-paint layout stands; nothing the user can act on.
      return;
    }
    const layout = resolveSidebarLayout(saved);
    // An edit made before the read landed wins over it; `save` will write it.
    if (sameSidebarLayout(this.layout(), this.persisted)) {
      this.layout.set(layout);
    }
    this.persisted = layout;
    writeCachedLayout(layout);
  }

  private isAvailable(def: SidebarItemDef): boolean {
    return !def.feature || this.features[def.feature] === true;
  }
}

function readCachedLayout(): SidebarItemPreference[] | null {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(LAYOUT_CACHE_KEY) ?? 'null');
    return Array.isArray(parsed)
      ? parsed.filter((pref): pref is SidebarItemPreference => typeof pref?.id === 'string')
      : null;
  } catch {
    return null;
  }
}

function writeCachedLayout(layout: SidebarItemPreference[]): void {
  try {
    localStorage.setItem(LAYOUT_CACHE_KEY, JSON.stringify(layout));
  } catch {
    // Storage full or blocked: the next load reads the account instead.
  }
}
