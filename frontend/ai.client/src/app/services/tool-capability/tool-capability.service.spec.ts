import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ToolCapabilityService } from './tool-capability.service';
import { ConfigService } from '../config.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';

describe('ToolCapabilityService', () => {
  let get: ReturnType<typeof vi.fn>;
  let post: ReturnType<typeof vi.fn>;

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
    post = vi.fn().mockReturnValue(of({ messages: [], truncated: false }));
    TestBed.configureTestingModule({
      providers: [
        { provide: HttpClient, useValue: { get, post } },
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
    expect(get).toHaveBeenCalledWith(
      'http://api/tools/canvas_faculty/capabilities',
      expect.anything(),
    );
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
    expect(get).toHaveBeenCalledWith(
      'http://api/tools/weird%2Fid/capabilities',
      expect.anything(),
    );
  });

  it('ignores an empty tool id', async () => {
    await service().ensure('');
    expect(get).not.toHaveBeenCalled();
  });

  it('opts the capabilities read out of the global error toast', async () => {
    await service().ensure('canvas_faculty');
    const options = get.mock.calls[0][1];
    expect(options.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
  });

  it('opts prompt resolution out of the global error toast', async () => {
    await service().resolvePrompt('canvas_faculty', 'summarize', {});
    const options = post.mock.calls[0][2];
    expect(options.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
  });
});
