import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { FEATURES } from '../../services/features';
import { SidebarItemPreference, UserSettings, UserSettingsService } from '../../services/user-settings.service';
import { SidebarLayoutService } from './sidebar-layout.service';

const CACHE_KEY = 'sidebar-layout';

describe('SidebarLayoutService', () => {
  let settings: { getSettings: ReturnType<typeof vi.fn>; updateSettings: ReturnType<typeof vi.fn> };
  let resolveSettings!: (value: UserSettings) => void;

  function setup(features = { projects: true, conversationSearch: false }): SidebarLayoutService {
    TestBed.configureTestingModule({
      providers: [
        { provide: UserSettingsService, useValue: settings },
        { provide: FEATURES, useValue: features },
      ],
    });
    return TestBed.inject(SidebarLayoutService);
  }

  /** Resolve the pending settings read and let the service apply it. */
  async function land(sidebarItems: SidebarItemPreference[] | null): Promise<void> {
    resolveSettings({ defaultModelId: null, sidebarItems });
    await Promise.resolve();
    await Promise.resolve();
  }

  const ids = (entries: { id: string }[]) => entries.map(entry => entry.id);

  beforeEach(() => {
    localStorage.removeItem(CACHE_KEY);
    settings = {
      getSettings: vi.fn(() => new Promise<UserSettings>(resolve => (resolveSettings = resolve))),
      updateSettings: vi.fn().mockResolvedValue({}),
    };
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.removeItem(CACHE_KEY);
  });

  it('starts from the default layout, with every entry shown', () => {
    const layout = setup();
    expect(ids(layout.visibleEntries())).toEqual(['agents', 'projects', 'artifacts', 'customize', 'schedules']);
    expect(layout.hiddenEntries()).toEqual([]);
    expect(layout.isDefault()).toBe(true);
  });

  it("applies the account's layout when it lands, and caches it for the next first paint", async () => {
    const layout = setup();
    await land([
      { id: 'artifacts', visible: true },
      { id: 'agents', visible: false },
      { id: 'schedules', visible: true },
      { id: 'projects', visible: true },
      { id: 'customize', visible: true },
    ]);
    expect(ids(layout.visibleEntries())).toEqual(['artifacts', 'schedules', 'projects', 'customize']);
    expect(ids(layout.hiddenEntries())).toEqual(['agents']);
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)!)[0]).toEqual({ id: 'artifacts', visible: true });
  });

  it('paints from the cache before the account answers', () => {
    localStorage.setItem(
      CACHE_KEY,
      JSON.stringify([
        { id: 'customize', visible: true },
        { id: 'agents', visible: true },
      ]),
    );
    const layout = setup();
    expect(layout.visibleEntries()[0].id).toBe('customize');
  });

  it('survives an unreadable cache', () => {
    localStorage.setItem(CACHE_KEY, '{not json');
    expect(ids(setup().visibleEntries())).toEqual(['agents', 'projects', 'artifacts', 'customize', 'schedules']);
  });

  it('keeps an edit made before the account answered, and saves it', async () => {
    const layout = setup();
    layout.setVisible('schedules', false);
    await land([{ id: 'agents', visible: false }]);
    expect(layout.visibleEntries().some(entry => entry.id === 'schedules')).toBe(false);
    expect(layout.visibleEntries().some(entry => entry.id === 'agents')).toBe(true);

    await layout.save();
    expect(settings.updateSettings).toHaveBeenCalledTimes(1);
  });

  it('leaves out an entry whose feature switch is off, and keeps its place across a move', async () => {
    const layout = setup({ projects: false, conversationSearch: false });
    await land(null);
    expect(ids(layout.entries())).toEqual(['agents', 'artifacts', 'customize', 'schedules']);

    // Customize to the top, by its position among the entries offered.
    layout.move(2, 0);
    expect(ids(layout.entries())).toEqual(['customize', 'agents', 'artifacts', 'schedules']);

    await layout.save();
    const saved: SidebarItemPreference[] = settings.updateSettings.mock.calls[0][0].sidebarItems;
    // Projects keeps the second slot it had, ready for a build that shows it.
    expect(ids(saved)).toEqual(['customize', 'projects', 'agents', 'artifacts', 'schedules']);
  });

  it('saves nothing when nothing changed', async () => {
    const layout = setup();
    await land([{ id: 'agents', visible: false }]);
    await layout.save();

    layout.setVisible('agents', true);
    layout.setVisible('agents', false);
    await layout.save();
    expect(settings.updateSettings).not.toHaveBeenCalled();
  });

  it('saves the default layout as null, so the user follows future defaults', async () => {
    const layout = setup();
    await land([{ id: 'agents', visible: false }]);
    layout.reset();
    expect(layout.isDefault()).toBe(true);

    await layout.save();
    expect(settings.updateSettings).toHaveBeenCalledWith({ sidebarItems: null });
  });

  it('saves only once for one change', async () => {
    const layout = setup();
    await land(null);
    layout.setVisible('schedules', false);
    await layout.save();
    await layout.save();
    expect(settings.updateSettings).toHaveBeenCalledTimes(1);
    expect(settings.updateSettings.mock.calls[0][0].sidebarItems).toContainEqual({ id: 'schedules', visible: false });
  });

  it('keeps the default layout when the settings read fails', async () => {
    settings.getSettings.mockRejectedValueOnce(new Error('503'));
    const layout = setup();
    await Promise.resolve();
    await Promise.resolve();
    expect(layout.isDefault()).toBe(true);
  });
});
