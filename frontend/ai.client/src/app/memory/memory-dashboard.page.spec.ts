import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ResourceRef, resource } from '@angular/core';
import { MemoryDashboardPage } from './memory-dashboard.page';
import { MemoryService } from './services/memory.service';
import { AllMemoriesResponse, MemoriesResponse, MemoryStatus } from './models/memory.model';
import { UserSettings, UserSettingsService } from '../services/user-settings.service';

function memories(namespace: string, ids: string[]): MemoriesResponse {
  return {
    namespace,
    totalCount: ids.length,
    memories: ids.map(id => ({ recordId: id, content: `content ${id}` })),
  };
}

describe('MemoryDashboardPage deleteMemory', () => {
  let service: {
    memoryStatus: ResourceRef<MemoryStatus | undefined>;
    allMemories: ResourceRef<AllMemoriesResponse | undefined>;
    deleteMemory: ReturnType<typeof vi.fn>;
    searchMemories: ReturnType<typeof vi.fn>;
    reload: ReturnType<typeof vi.fn>;
  };
  let settings: {
    settingsResource: ResourceRef<UserSettings | undefined>;
    updateSettings: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    // Real resources (so update() behaves as in the app), built in a factory
    // because resource() needs an injection context.
    const createService = () => ({
      memoryStatus: resource({
        loader: async () => ({ status: 'available', available: true, mode: 'cloud' }) as MemoryStatus,
      }),
      allMemories: resource({
        loader: async () => ({
          preferences: memories('prefs', ['p1', 'p2']),
          facts: memories('facts', ['f1']),
        }),
      }),
      deleteMemory: vi.fn().mockResolvedValue({ deletedCount: 1, message: 'ok' }),
      searchMemories: vi.fn(),
      reload: vi.fn(),
    });

    TestBed.configureTestingModule({
      imports: [MemoryDashboardPage],
      providers: [
        { provide: MemoryService, useFactory: createService },
        {
          provide: UserSettingsService,
          useFactory: () => ({
            settingsResource: resource({ loader: async () => ({ defaultModelId: null }) as UserSettings }),
            updateSettings: vi.fn().mockResolvedValue({ defaultModelId: null, memoryEnabled: false }),
          }),
        },
      ],
    });
    service = TestBed.inject(MemoryService) as unknown as typeof service;
    settings = TestBed.inject(UserSettingsService) as unknown as typeof settings;
    await vi.waitFor(() => expect(service.allMemories.hasValue()).toBe(true));
  });

  it('removes the deleted memory from the displayed list without reloading', async () => {
    const fixture = TestBed.createComponent(MemoryDashboardPage);
    const page = fixture.componentInstance;
    fixture.detectChanges();
    expect(page.preferences().map(m => m.recordId)).toEqual(['p1', 'p2']);

    await page.deleteMemory('p1');
    fixture.detectChanges();

    expect(service.deleteMemory).toHaveBeenCalledWith('p1');
    expect(service.reload).not.toHaveBeenCalled();
    expect(page.preferences().map(m => m.recordId)).toEqual(['p2']);
    expect(page.preferencesCount()).toBe(1);
    expect(page.facts().map(m => m.recordId)).toEqual(['f1']);
    expect(page.deletingMemoryId()).toBeNull();
    expect((fixture.nativeElement as HTMLElement).textContent).not.toContain('content p1');
  });

  it('removes the deleted memory from active search results', async () => {
    const fixture = TestBed.createComponent(MemoryDashboardPage);
    const page = fixture.componentInstance;
    page.searchResults.set(memories('search', ['f1', 'p2']));

    await page.deleteMemory('f1');

    expect(page.searchResults()?.memories.map(m => m.recordId)).toEqual(['p2']);
    expect(page.searchResults()?.totalCount).toBe(1);
    expect(page.facts()).toEqual([]);
  });

  it('turns long-term memory off through the user settings', async () => {
    await vi.waitFor(() => expect(service.memoryStatus.hasValue()).toBe(true));
    await vi.waitFor(() => expect(settings.settingsResource.hasValue()).toBe(true));
    const fixture = TestBed.createComponent(MemoryDashboardPage);
    fixture.detectChanges();
    const toggle = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('button[role="switch"]')!;
    expect(toggle.getAttribute('aria-checked')).toBe('true');

    toggle.click();
    await fixture.whenStable();

    expect(settings.updateSettings).toHaveBeenCalledWith({ memoryEnabled: false });
  });

  it('keeps the memory when the delete request fails', async () => {
    service.deleteMemory.mockRejectedValueOnce(new Error('boom'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const page = TestBed.createComponent(MemoryDashboardPage).componentInstance;

    await page.deleteMemory('p1');

    expect(page.preferences().map(m => m.recordId)).toEqual(['p1', 'p2']);
    expect(page.deletingMemoryId()).toBeNull();
  });
});
