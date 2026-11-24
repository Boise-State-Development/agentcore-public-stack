import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { LocalSettingsService } from './local-settings.service';

describe('LocalSettingsService', () => {
  beforeEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
  });

  describe('collapsedSidebarGroups', () => {
    it('starts with every group open', () => {
      expect(TestBed.inject(LocalSettingsService).collapsedSidebarGroups().size).toBe(0);
    });

    it('toggles a group and survives a reload', () => {
      const settings = TestBed.inject(LocalSettingsService);
      settings.toggleSidebarGroup('Yesterday');
      settings.toggleSidebarGroup('Pinned');
      settings.toggleSidebarGroup('Pinned');

      TestBed.resetTestingModule();
      TestBed.configureTestingModule({});
      expect([...TestBed.inject(LocalSettingsService).collapsedSidebarGroups()]).toEqual(['Yesterday']);
    });

    it('ignores a stored value it cannot read', () => {
      localStorage.setItem('sidebar-collapsed-groups', '{not json');
      expect(TestBed.inject(LocalSettingsService).collapsedSidebarGroups().size).toBe(0);
      localStorage.setItem('sidebar-collapsed-groups', '["Today", 3]');
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({});
      expect([...TestBed.inject(LocalSettingsService).collapsedSidebarGroups()]).toEqual(['Today']);
    });
  });

  describe('agentsViewMode', () => {
    it('defaults to grid', () => {
      // Grid shows an agent's artwork at a size you recognise, and most people have few
      // enough agents that density is not yet the problem.
      expect(TestBed.inject(LocalSettingsService).agentsViewMode()).toBe('grid');
    });

    it('survives a reload', () => {
      TestBed.inject(LocalSettingsService).setAgentsViewMode('list');

      TestBed.resetTestingModule();
      TestBed.configureTestingModule({});
      expect(TestBed.inject(LocalSettingsService).agentsViewMode()).toBe('list');
    });

    it('falls back to grid on a value it does not recognise', () => {
      // Stored raw rather than JSON-encoded, so anything hand-edited or left by an older
      // build lands on the default instead of rendering an undefined layout.
      localStorage.setItem('agents-view-mode', 'compact');
      expect(TestBed.inject(LocalSettingsService).agentsViewMode()).toBe('grid');
    });
  });
});
