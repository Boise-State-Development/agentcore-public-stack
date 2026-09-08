import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { McpAppStateService } from './mcp-app-state.service';
import type { UiResourceEvent } from '../../../shared/utils/stream-parser';

const SESSION_A = 'sess-a';
const SESSION_B = 'sess-b';

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
    expect(svc.has(SESSION_A, 'tu-1')).toBe(false);
    expect(svc.get(SESSION_A, 'tu-1')).toBeUndefined();
  });

  it('records and retrieves a resource by toolUseId', () => {
    const e = ev('tu-1');
    svc.recordLive(SESSION_A, e);
    expect(svc.has(SESSION_A, 'tu-1')).toBe(true);
    expect(svc.get(SESSION_A, 'tu-1')).toEqual(e);
  });

  it('last write wins for the same toolUseId', () => {
    svc.recordLive(SESSION_A, ev('tu-1', '<old>'));
    svc.recordLive(SESSION_A, ev('tu-1', '<new>'));
    expect(svc.get(SESSION_A, 'tu-1')?.html).toBe('<new>');
  });

  it('keeps distinct invocations separate', () => {
    svc.recordLive(SESSION_A, ev('tu-1'));
    svc.recordLive(SESSION_A, ev('tu-2'));
    expect(svc.get(SESSION_A, 'tu-1')?.resourceUri).toBe('ui://srv/tu-1');
    expect(svc.get(SESSION_A, 'tu-2')?.resourceUri).toBe('ui://srv/tu-2');
  });

  it('reset() drops everything (full teardown)', () => {
    svc.recordLive(SESSION_A, ev('tu-1'));
    svc.recordPartialInput(SESSION_A, 'tu-1', { elements: [] });
    svc.reset();
    expect(svc.has(SESSION_A, 'tu-1')).toBe(false);
    expect(svc.getPartialInput(SESSION_A, 'tu-1')).toBeUndefined();
  });

  it('treats a null session id as empty (no viewed conversation)', () => {
    svc.recordLive(SESSION_A, ev('tu-1'));
    expect(svc.has(null, 'tu-1')).toBe(false);
    expect(svc.get(null, 'tu-1')).toBeUndefined();
    expect(svc.getPartialInput(null, 'tu-1')).toBeUndefined();
  });

  describe('per-conversation retention', () => {
    // The regression this shape exists to prevent: leaving a conversation
    // and coming back dropped every App frame to a plain tool card, because
    // the registry was reset on navigation while `loadMessagesForSession`
    // skips the `GET /messages` (and its `uiResources` sidecar) for a
    // conversation whose messages are already cached.
    it('keeps a conversation\'s resources across a visit to another one', () => {
      svc.seedFromHydration(SESSION_A, [ev('tu-1')]);
      svc.recordLive(SESSION_A, ev('tu-2'));

      // User navigates to B (which has its own App), then back to A.
      svc.recordLive(SESSION_B, ev('tu-3'));

      expect(svc.has(SESSION_A, 'tu-1')).toBe(true);
      expect(svc.has(SESSION_A, 'tu-2')).toBe(true);
    });

    it('scopes lookups to their own conversation', () => {
      svc.recordLive(SESSION_A, ev('tu-1'));
      expect(svc.has(SESSION_B, 'tu-1')).toBe(false);
      expect(svc.get(SESSION_B, 'tu-1')).toBeUndefined();
    });

    it('retains a background stream\'s resource for when the user opens it', () => {
      // recordLive is no longer viewed-session-gated: a conversation
      // streaming in the background records into its own bucket.
      svc.recordLive(SESSION_B, ev('tu-9'));
      expect(svc.get(SESSION_B, 'tu-9')?.resourceUri).toBe('ui://srv/tu-9');
    });
  });

  describe('recordPartialInput', () => {
    it('records and retrieves the latest streamed partial input', () => {
      expect(svc.getPartialInput(SESSION_A, 'tu-1')).toBeUndefined();
      svc.recordPartialInput(SESSION_A, 'tu-1', {
        elements: [{ type: 'rect' }],
      });
      expect(svc.getPartialInput(SESSION_A, 'tu-1')).toEqual({
        elements: [{ type: 'rect' }],
      });
    });

    it('last write wins (the backend streams a growing healed prefix)', () => {
      svc.recordPartialInput(SESSION_A, 'tu-1', {
        elements: [{ type: 'rect' }],
      });
      svc.recordPartialInput(SESSION_A, 'tu-1', {
        elements: [{ type: 'rect' }, { type: 'cameraUpdate' }],
      });
      expect(
        (svc.getPartialInput(SESSION_A, 'tu-1')?.['elements'] as unknown[])
          .length,
      ).toBe(2);
    });

    it('keeps partial input separate per toolUseId', () => {
      svc.recordPartialInput(SESSION_A, 'tu-1', { a: 1 });
      svc.recordPartialInput(SESSION_A, 'tu-2', { b: 2 });
      expect(svc.getPartialInput(SESSION_A, 'tu-1')).toEqual({ a: 1 });
      expect(svc.getPartialInput(SESSION_A, 'tu-2')).toEqual({ b: 2 });
    });

    it('keeps partial input separate per conversation', () => {
      svc.recordPartialInput(SESSION_A, 'tu-1', { a: 1 });
      expect(svc.getPartialInput(SESSION_B, 'tu-1')).toBeUndefined();
    });
  });

  describe('seedFromHydration', () => {
    it('seeds persisted resources so the frame re-renders on reload', () => {
      svc.seedFromHydration(SESSION_A, [ev('tu-1'), ev('tu-2')]);
      expect(svc.has(SESSION_A, 'tu-1')).toBe(true);
      expect(svc.get(SESSION_A, 'tu-2')?.resourceUri).toBe('ui://srv/tu-2');
    });

    it('is a no-op for an empty list', () => {
      svc.seedFromHydration(SESSION_A, []);
      expect(svc.has(SESSION_A, 'tu-1')).toBe(false);
    });

    it('does not clobber a live recordLive entry (non-clobbering)', () => {
      svc.recordLive(SESSION_A, ev('tu-1', '<live>'));
      // A slow hydration response arriving after the live event must not
      // overwrite the fresher live resource.
      svc.seedFromHydration(SESSION_A, [ev('tu-1', '<stale>')]);
      expect(svc.get(SESSION_A, 'tu-1')?.html).toBe('<live>');
    });

    it('seeds into the named conversation only', () => {
      svc.seedFromHydration(SESSION_A, [ev('tu-1')]);
      expect(svc.has(SESSION_B, 'tu-1')).toBe(false);
    });
  });
});
