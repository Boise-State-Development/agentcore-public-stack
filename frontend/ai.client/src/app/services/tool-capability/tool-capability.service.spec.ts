import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ToolCapabilityService } from './tool-capability.service';
import { ConfigService } from '../config.service';

describe('ToolCapabilityService', () => {
  let get: ReturnType<typeof vi.fn>;

  const snapshot = (over: Partial<any> = {}) => ({
    toolId: 'canvas_faculty',
    prompts: [],
    resources: [],
    supportsPrompts: false,
    supportsResources: false,
    discoveredAt: '2026-09-01T00:00:00Z',
    discoveredBy: 'admin',
    error: null,
    truncated: false,
    ...over,
  });

  beforeEach(() => {
    TestBed.resetTestingModule();
    get = vi.fn().mockReturnValue(of(snapshot()));
    TestBed.configureTestingModule({
      providers: [
        { provide: HttpClient, useValue: { get } },
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://api' } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  const service = () => TestBed.inject(ToolCapabilityService);

  it('returns null before anything is loaded', () => {
    expect(service().capabilitiesFor('canvas_faculty')).toBeNull();
  });

  it('loads and caches a snapshot', async () => {
    const s = service();
    await s.ensure('canvas_faculty');
    expect(s.capabilitiesFor('canvas_faculty')?.toolId).toBe('canvas_faculty');
    expect(get).toHaveBeenCalledWith('http://api/tools/canvas_faculty/capabilities');
  });

  it('fetches each tool once', async () => {
    const s = service();
    await s.ensure('canvas_faculty');
    await s.ensure('canvas_faculty');
    expect(get).toHaveBeenCalledTimes(1);
  });

  it('does not retry a failed read on every render', async () => {
    // The detail pane re-reads this on each change detection; retrying there
    // would turn one dead endpoint into a request loop.
    get.mockReturnValue(throwError(() => new Error('404')));
    const s = service();
    await s.ensure('canvas_faculty');
    await s.ensure('canvas_faculty');
    expect(get).toHaveBeenCalledTimes(1);
    expect(s.hasFailed('canvas_faculty')).toBe(true);
    expect(s.capabilitiesFor('canvas_faculty')).toBeNull();
  });

  it('re-reads after invalidate', async () => {
    const s = service();
    await s.ensure('canvas_faculty');
    s.invalidate('canvas_faculty');
    await s.ensure('canvas_faculty');
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('encodes the tool id in the URL', async () => {
    await service().ensure('weird/id');
    expect(get).toHaveBeenCalledWith('http://api/tools/weird%2Fid/capabilities');
  });

  it('ignores an empty tool id', async () => {
    await service().ensure('');
    expect(get).not.toHaveBeenCalled();
  });
});
