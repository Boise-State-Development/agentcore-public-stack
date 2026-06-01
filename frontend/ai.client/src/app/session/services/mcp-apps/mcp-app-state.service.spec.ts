import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { McpAppStateService } from './mcp-app-state.service';
import type { UiResourceEvent } from '../../../shared/utils/stream-parser';

function ev(toolUseId: string, html = '<h1>hi</h1>'): UiResourceEvent {
  return {
    type: 'ui_resource',
    toolUseId,
    resourceUri: `ui://srv/${toolUseId}`,
    html,
    mimeType: 'text/html;profile=mcp-app',
    csp: {},
    permissions: {},
    sandboxOrigin: 'https://mcp-sandbox.example.com',
  };
}

describe('McpAppStateService', () => {
  let svc: McpAppStateService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    svc = TestBed.inject(McpAppStateService);
  });

  it('starts empty', () => {
    expect(svc.hasApps()).toBe(false);
    expect(svc.has('tu-1')).toBe(false);
    expect(svc.get('tu-1')).toBeUndefined();
  });

  it('records and retrieves a resource by toolUseId', () => {
    const e = ev('tu-1');
    svc.recordLive(e);
    expect(svc.has('tu-1')).toBe(true);
    expect(svc.get('tu-1')).toEqual(e);
    expect(svc.hasApps()).toBe(true);
  });

  it('last write wins for the same toolUseId', () => {
    svc.recordLive(ev('tu-1', '<old>'));
    svc.recordLive(ev('tu-1', '<new>'));
    expect(svc.get('tu-1')?.html).toBe('<new>');
  });

  it('keeps distinct invocations separate', () => {
    svc.recordLive(ev('tu-1'));
    svc.recordLive(ev('tu-2'));
    expect(svc.get('tu-1')?.resourceUri).toBe('ui://srv/tu-1');
    expect(svc.get('tu-2')?.resourceUri).toBe('ui://srv/tu-2');
  });

  it('reset() drops everything (conversation teardown)', () => {
    svc.recordLive(ev('tu-1'));
    svc.recordPartialInput('tu-1', { elements: [] });
    svc.reset();
    expect(svc.hasApps()).toBe(false);
    expect(svc.has('tu-1')).toBe(false);
    expect(svc.getPartialInput('tu-1')).toBeUndefined();
  });

  describe('recordPartialInput', () => {
    it('records and retrieves the latest streamed partial input', () => {
      expect(svc.getPartialInput('tu-1')).toBeUndefined();
      svc.recordPartialInput('tu-1', { elements: [{ type: 'rect' }] });
      expect(svc.getPartialInput('tu-1')).toEqual({
        elements: [{ type: 'rect' }],
      });
    });

    it('last write wins (the backend streams a growing healed prefix)', () => {
      svc.recordPartialInput('tu-1', { elements: [{ type: 'rect' }] });
      svc.recordPartialInput('tu-1', {
        elements: [{ type: 'rect' }, { type: 'cameraUpdate' }],
      });
      expect(
        (svc.getPartialInput('tu-1')?.['elements'] as unknown[]).length,
      ).toBe(2);
    });

    it('keeps partial input separate per toolUseId', () => {
      svc.recordPartialInput('tu-1', { a: 1 });
      svc.recordPartialInput('tu-2', { b: 2 });
      expect(svc.getPartialInput('tu-1')).toEqual({ a: 1 });
      expect(svc.getPartialInput('tu-2')).toEqual({ b: 2 });
    });
  });

  describe('seedFromHydration', () => {
    it('seeds persisted resources so the frame re-renders on reload', () => {
      svc.seedFromHydration([ev('tu-1'), ev('tu-2')]);
      expect(svc.has('tu-1')).toBe(true);
      expect(svc.get('tu-2')?.resourceUri).toBe('ui://srv/tu-2');
      expect(svc.hasApps()).toBe(true);
    });

    it('is a no-op for an empty list', () => {
      svc.seedFromHydration([]);
      expect(svc.hasApps()).toBe(false);
    });

    it('does not clobber a live recordLive entry (non-clobbering)', () => {
      svc.recordLive(ev('tu-1', '<live>'));
      // A slow hydration response arriving after the live event must not
      // overwrite the fresher live resource.
      svc.seedFromHydration([ev('tu-1', '<stale>')]);
      expect(svc.get('tu-1')?.html).toBe('<live>');
    });
  });
});
