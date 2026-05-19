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
    svc.reset();
    expect(svc.hasApps()).toBe(false);
    expect(svc.has('tu-1')).toBe(false);
  });
});
