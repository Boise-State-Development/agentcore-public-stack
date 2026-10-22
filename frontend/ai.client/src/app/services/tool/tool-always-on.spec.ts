import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ToolService, Tool } from './tool.service';

/**
 * PR-3 of docs/specs/admin-always-on-tools.md — the picker lock.
 *
 * The backend unions pinned tools into every turn regardless of what the
 * picker stores, so a toggle here could only produce a picker that disagrees
 * with the turn. The UI disables the control; these guard the keyboard and
 * programmatic paths behind it, which never see a `disabled` attribute.
 */
describe('ToolService — always-on tools', () => {
  let service: ToolService;
  let http: HttpTestingController;

  function makeTool(over: Partial<Tool> = {}): Tool {
    return {
      toolId: 'kb_search',
      displayName: 'KB Search',
      description: 'Search the KB',
      category: 'search',
      icon: null,
      protocol: 'local',
      status: 'active',
      grantedBy: ['staff'],
      enabledByDefault: true,
      userEnabled: null,
      isEnabled: true,
      ...over,
    } as Tool;
  }

  /**
   * Flush the catalog fetch the service kicks off in its own constructor, then
   * let its promise settle. Awaiting matters: without it `tools()` is still
   * empty and every guard under test returns early on a missing tool, so the
   * specs pass for the wrong reason.
   */
  async function load(tools: Tool[]): Promise<void> {
    const req = http.expectOne(r => r.url.includes('/tools'));
    req.flush({ tools, categories: [], appRolesApplied: [] });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(service.tools().length).toBe(tools.length);
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [ToolService, provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(ToolService);
    http = TestBed.inject(HttpTestingController);
  });

  it('ignores a toggle on a pinned tool and saves nothing', async () => {
    await load([makeTool({ alwaysOn: true })]);
    const save = vi.spyOn(service, 'savePreferences');

    await service.toggleTool('kb_search', { respectAgentLock: false });

    expect(save).not.toHaveBeenCalled();
    expect(service.tools()[0].isEnabled).toBe(true);
  });

  it('still toggles an ordinary tool', async () => {
    await load([makeTool({ toolId: 'browse_web', alwaysOn: false })]);
    const save = vi.spyOn(service, 'savePreferences').mockResolvedValue(undefined);

    await service.toggleTool('browse_web', { respectAgentLock: false });

    expect(save).toHaveBeenCalledWith({ browse_web: false });
    expect(service.tools()[0].isEnabled).toBe(false);
  });

  it('treats an older backend that omits alwaysOn as unlocked', async () => {
    // §10.1 — new bundle, old backend: `undefined` -> falsy -> today's toggle.
    await load([makeTool({ toolId: 'browse_web' })]);
    const save = vi.spyOn(service, 'savePreferences').mockResolvedValue(undefined);

    await service.toggleTool('browse_web', { respectAgentLock: false });

    expect(save).toHaveBeenCalled();
  });

  it('ignores a toggle on a pinned sub-tool', async () => {
    await load([
      makeTool({
        toolId: 'weather',
        serverTools: [
          { name: 'get_forecast', enabled: true, alwaysOn: true },
          { name: 'get_history', enabled: true },
        ],
      }),
    ]);
    const save = vi.spyOn(service, 'savePreferences');

    await service.toggleServerTool('weather', 'get_forecast', { respectAgentLock: false });

    expect(save).not.toHaveBeenCalled();
  });

  it('switching a server off leaves its pinned tool on', async () => {
    await load([
      makeTool({
        toolId: 'weather',
        serverTools: [
          { name: 'get_forecast', enabled: true, alwaysOn: true },
          { name: 'get_history', enabled: true },
        ],
      }),
    ]);
    const save = vi.spyOn(service, 'savePreferences').mockResolvedValue(undefined);

    await service.toggleTool('weather', { respectAgentLock: false });

    // The pinned tool is not sent as `false` — the backend would drop it (D6),
    // and omitting it keeps the optimistic state and the saved state in
    // agreement.
    expect(save).toHaveBeenCalledWith({
      weather: false,
      'weather::get_history': false,
    });
    const row = service.tools()[0];
    expect(row.serverTools?.find(s => s.name === 'get_forecast')?.enabled).toBe(true);
    expect(row.serverTools?.find(s => s.name === 'get_history')?.enabled).toBe(false);
    // Still effectively on: one of its tools cannot be turned off.
    expect(row.isEnabled).toBe(true);
  });

  it('keeps a pinned tool in the ids sent to the agent', async () => {
    await load([makeTool({ alwaysOn: true }), makeTool({ toolId: 'browse_web', isEnabled: false })]);
    expect(service.getEnabledToolIds()).toEqual(['kb_search']);
  });
});
