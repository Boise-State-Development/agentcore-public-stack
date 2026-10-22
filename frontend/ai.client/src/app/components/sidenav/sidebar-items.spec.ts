import { describe, expect, it } from 'vitest';
import {
  SIDEBAR_ITEMS,
  SidebarItemDef,
  defaultSidebarLayout,
  resolveSidebarLayout,
  sameSidebarLayout,
} from './sidebar-items';

const DEFS: SidebarItemDef[] = [
  { id: 'a', label: 'A', route: '/a', icon: 'heroSparkles', defaultVisible: true },
  { id: 'b', label: 'B', route: '/b', icon: 'heroSparkles', defaultVisible: true },
  { id: 'c', label: 'C', route: '/c', icon: 'heroSparkles', defaultVisible: false },
];

describe('resolveSidebarLayout', () => {
  it('is the default layout for a user who never saved one', () => {
    expect(resolveSidebarLayout(null, DEFS)).toEqual(defaultSidebarLayout(DEFS));
    expect(resolveSidebarLayout([], DEFS)).toEqual(defaultSidebarLayout(DEFS));
  });

  it("keeps the user's order and visibility", () => {
    const saved = [
      { id: 'c', visible: true },
      { id: 'a', visible: false },
      { id: 'b', visible: true },
    ];
    expect(resolveSidebarLayout(saved, DEFS)).toEqual(saved);
  });

  it('drops ids this build does not define, and duplicates', () => {
    const saved = [
      { id: 'gone', visible: true },
      { id: 'b', visible: false },
      { id: 'b', visible: true },
      { id: 'a', visible: true },
      { id: 'c', visible: false },
    ];
    expect(resolveSidebarLayout(saved, DEFS)).toEqual([
      { id: 'b', visible: false },
      { id: 'a', visible: true },
      { id: 'c', visible: false },
    ]);
  });

  it('puts a new entry after its default predecessor, with its default visibility', () => {
    // Saved before `b` existed, and with the order changed.
    const saved = [
      { id: 'c', visible: true },
      { id: 'a', visible: true },
    ];
    expect(resolveSidebarLayout(saved, DEFS)).toEqual([
      { id: 'c', visible: true },
      { id: 'a', visible: true },
      { id: 'b', visible: true },
    ]);
  });

  it('puts a new first entry at the top', () => {
    expect(resolveSidebarLayout([{ id: 'b', visible: true }, { id: 'c', visible: true }], DEFS)).toEqual([
      { id: 'a', visible: true },
      { id: 'b', visible: true },
      { id: 'c', visible: true },
    ]);
  });
});

describe('SIDEBAR_ITEMS', () => {
  it('has unique ids the settings API accepts', () => {
    const ids = SIDEBAR_ITEMS.map(def => def.id);
    expect(new Set(ids).size).toBe(ids.length);
    // `SIDEBAR_ITEM_ID_PATTERN` in apis/shared/user_settings/models.py.
    for (const id of ids) {
      expect(id).toMatch(/^[a-z][a-z0-9-]{0,39}$/);
    }
  });

  it('shows every entry by default', () => {
    expect(SIDEBAR_ITEMS.filter(def => def.defaultVisible).map(def => def.id)).toEqual([
      'agents',
      'projects',
      'artifacts',
      'customize',
      'schedules',
    ]);
  });
});

describe('sameSidebarLayout', () => {
  it('compares order and visibility', () => {
    const a = [{ id: 'a', visible: true }, { id: 'b', visible: false }];
    expect(sameSidebarLayout(a, [...a])).toBe(true);
    expect(sameSidebarLayout(a, [a[1], a[0]])).toBe(false);
    expect(sameSidebarLayout(a, [a[0], { id: 'b', visible: true }])).toBe(false);
    expect(sameSidebarLayout(a, [a[0]])).toBe(false);
  });
});
