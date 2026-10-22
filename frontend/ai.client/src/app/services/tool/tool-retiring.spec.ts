import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ToolService, Tool, isRetiring } from './tool.service';

/**
 * §7 of docs/specs/mcp-server-retirement.md — the chat picker's retirement guard.
 *
 * The asymmetry is the whole point and is the OPPOSITE of always-on's: a pinned
 * tool is on and cannot be turned off; a retiring tool can be turned off and
 * cannot be turned back on. Turning one off is precisely the action retirement
 * is asking for, so that direction must stay open — a guard that blocked both
 * would strand every user who already has the tool.
 *
 * None of this is an access decision. `can_access_tool` still admits a retiring
 * tool and an Agent that binds it still runs; these only stop NEW adoption.
 */
describe('ToolService — retiring tools', () => {
  let service: ToolService;
  let http: HttpTestingController;

  function makeTool(over: Partial<Tool> = {}): Tool {
    return {
      toolId: 'canvas_faculty',
      displayName: 'Canvas',
      description: 'Canvas LMS',
      category: 'gateway',
      icon: null,
      protocol: 'mcp_external',
      status: 'active',
      grantedBy: ['staff'],
      enabledByDefault: false,
      userEnabled: null,
      isEnabled: false,
      ...over,
    } as Tool;
  }

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

  describe('isRetiring', () => {
    it.each(['deprecated', 'disabled', 'coming_soon'] as const)('is true for %s', status => {
      expect(isRetiring(makeTool({ status }))).toBe(true);
    });

    it('is false for active', () => {
      expect(isRetiring(makeTool({ status: 'active' }))).toBe(false);
    });

    it('treats an absent status as active', () => {
      // An older backend omits it. Reading `undefined` as "retiring" would lock
      // every picker in the app on a version skew.
      expect(isRetiring({ status: undefined as unknown as Tool['status'] })).toBe(false);
    });
  });

  it('refuses to turn a retiring tool ON, and saves nothing', async () => {
    await load([makeTool({ status: 'deprecated', isEnabled: false })]);
    const save = vi.spyOn(service, 'savePreferences');

    await service.toggleTool('canvas_faculty', { respectAgentLock: false });

    expect(save).not.toHaveBeenCalled();
    expect(service.tools()[0].isEnabled).toBe(false);
  });

  it('still lets a user turn a retiring tool OFF', async () => {
    await load([makeTool({ status: 'deprecated', isEnabled: true, userEnabled: true })]);
    const save = vi.spyOn(service, 'savePreferences').mockResolvedValue(undefined);

    await service.toggleTool('canvas_faculty', { respectAgentLock: false });

    expect(save).toHaveBeenCalledWith({ canvas_faculty: false });
    expect(service.tools()[0].isEnabled).toBe(false);
  });

  it('refuses to adopt a retiring server one sub-tool at a time', async () => {
    // Retirement is a property of the SERVER — `can_access_tool` keys on the
    // base id — so an off server cannot be re-entered through its tool list.
    await load([
      makeTool({
        status: 'deprecated',
        isEnabled: false,
        serverTools: [
          { name: 'list_assignments', enabled: false },
          { name: 'get_rubric', enabled: false },
        ],
      }),
    ]);
    const save = vi.spyOn(service, 'savePreferences');

    await service.toggleServerTool('canvas_faculty', 'list_assignments', {
      respectAgentLock: false,
    });

    expect(save).not.toHaveBeenCalled();
  });

  it('still lets a user narrow a retiring server they already have on', async () => {
    await load([
      makeTool({
        status: 'deprecated',
        isEnabled: true,
        serverTools: [
          { name: 'list_assignments', enabled: true },
          { name: 'get_rubric', enabled: true },
        ],
      }),
    ]);
    const save = vi.spyOn(service, 'savePreferences').mockResolvedValue(undefined);

    await service.toggleServerTool('canvas_faculty', 'list_assignments', {
      respectAgentLock: false,
    });

    expect(save).toHaveBeenCalledWith({ 'canvas_faculty::list_assignments': false });
  });

  it('leaves an active tool completely alone', async () => {
    await load([makeTool({ status: 'active', isEnabled: false })]);
    const save = vi.spyOn(service, 'savePreferences').mockResolvedValue(undefined);

    await service.toggleTool('canvas_faculty', { respectAgentLock: false });

    expect(save).toHaveBeenCalledWith({ canvas_faculty: true });
  });

  it('does not drop a retiring tool the user already has from the turn', async () => {
    // The guard is about the picker, never about what runs. A user whose
    // preference still says ON keeps sending the id until they turn it off.
    await load([makeTool({ status: 'deprecated', isEnabled: true })]);
    expect(service.getEnabledToolIds()).toContain('canvas_faculty');
  });
});
