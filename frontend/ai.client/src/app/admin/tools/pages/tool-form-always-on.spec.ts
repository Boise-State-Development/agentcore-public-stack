import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { ToolFormPage } from './tool-form.page';
import { AdminToolService } from '../services/admin-tool.service';
import { ConnectorsService } from '../../connectors/services/connectors.service';
import { toolEnablementOf, toolEnablementFlags } from '../models/admin-tool.model';

/**
 * PR-1 of docs/specs/admin-always-on-tools.md — the admin surface for the
 * `alwaysOn` catalog flag.
 *
 * The backend still stores two booleans for backward compatibility (§2.2); the
 * form binds a single three-way so the incoherent pair (off by default + always
 * on) is unreachable from the UI. These tests guard that projection in both
 * directions, and the confirmation gate on pinning a whole MCP server.
 */
describe('ToolFormPage — always-on enablement', () => {
  let adminToolService: {
    createTool: ReturnType<typeof vi.fn>;
    updateTool: ReturnType<typeof vi.fn>;
    fetchTool: ReturnType<typeof vi.fn>;
    discoverMCPTools: ReturnType<typeof vi.fn>;
  };

  function makeComponent(): ToolFormPage {
    adminToolService = {
      createTool: vi.fn().mockResolvedValue({}),
      updateTool: vi.fn().mockResolvedValue({}),
      fetchTool: vi.fn(),
      discoverMCPTools: vi.fn().mockResolvedValue({ tools: [] }),
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ToolFormPage],
      providers: [
        provideRouter([]),
        { provide: AdminToolService, useValue: adminToolService },
        { provide: ConnectorsService, useValue: { getEnabledConnectors: () => [] } },
      ],
    });
    const cmp = TestBed.createComponent(ToolFormPage).componentInstance;
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    return cmp;
  }

  afterEach(() => TestBed.resetTestingModule());

  function fillLocalTool(cmp: ToolFormPage): void {
    cmp.form.patchValue({
      toolId: 'lookup_records',
      displayName: 'Lookup records',
      description: 'Looks things up',
      protocol: 'local',
    });
  }

  describe('projection between the three-way and the stored booleans', () => {
    it('derives user_choice from a tool that is off by default', () => {
      expect(toolEnablementOf({ enabledByDefault: false, alwaysOn: false })).toBe('user_choice');
    });

    it('derives default_on from a tool that is on by default', () => {
      expect(toolEnablementOf({ enabledByDefault: true, alwaysOn: false })).toBe('default_on');
    });

    it('derives always_on whenever the flag is set', () => {
      expect(toolEnablementOf({ enabledByDefault: true, alwaysOn: true })).toBe('always_on');
    });

    it('treats an older backend that omits alwaysOn as today behaviour', () => {
      // §10.1: new SPA bundle against an old backend reads undefined -> falsy.
      expect(toolEnablementOf({ enabledByDefault: true })).toBe('default_on');
      expect(toolEnablementOf({})).toBe('user_choice');
    });

    it('never projects the incoherent pair back onto the booleans', () => {
      expect(toolEnablementFlags('user_choice')).toEqual({
        enabledByDefault: false,
        alwaysOn: false,
      });
      expect(toolEnablementFlags('default_on')).toEqual({
        enabledByDefault: true,
        alwaysOn: false,
      });
      // The whole point: always_on implies enabledByDefault.
      expect(toolEnablementFlags('always_on')).toEqual({
        enabledByDefault: true,
        alwaysOn: true,
      });
    });
  });

  describe('submit payload', () => {
    it('sends both booleans set when the admin picks always on', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      fillLocalTool(cmp);
      cmp.form.patchValue({ toolEnablement: 'always_on' });

      await cmp.onSubmit();

      const payload = adminToolService.createTool.mock.calls[0][0];
      expect(payload.alwaysOn).toBe(true);
      expect(payload.enabledByDefault).toBe(true);
    });

    it('sends alwaysOn false for an ordinary default-on tool', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      fillLocalTool(cmp);
      cmp.form.patchValue({ toolEnablement: 'default_on' });

      await cmp.onSubmit();

      const payload = adminToolService.createTool.mock.calls[0][0];
      expect(payload.alwaysOn).toBe(false);
      expect(payload.enabledByDefault).toBe(true);
    });
  });

  describe('granting-role warning', () => {
    it('reports no granting role for a fresh non-public tool', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      fillLocalTool(cmp);
      expect(cmp.hasGrantingRole()).toBe(false);
    });

    it('counts isPublic as a grant, matching the backend grant set', async () => {
      // AppRoleService unions the public tools into the caller's role grant, so
      // a public tool does reach users even with zero roles listed.
      const cmp = makeComponent();
      await cmp.ngOnInit();
      fillLocalTool(cmp);
      cmp.form.patchValue({ isPublic: true });
      expect(cmp.hasGrantingRole()).toBe(true);
    });
  });

  describe('pinning a whole MCP server requires confirmation', () => {
    function makeServerForm(cmp: ToolFormPage, toolCount: number): void {
      cmp.form.patchValue({
        toolId: 'weather_mcp',
        displayName: 'Weather',
        description: 'Weather server',
        protocol: 'mcp_external',
        mcpServerUrl: 'https://example.com/mcp',
      });
      for (let i = 0; i < toolCount; i++) {
        cmp.addMcpTool();
        cmp.mcpToolsArray.at(i).patchValue({ name: `tool_${i}` });
      }
    }

    it('counts nothing while the tool is not always on', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      makeServerForm(cmp, 3);
      expect(cmp.alwaysOnServerToolCount()).toBe(0);
      expect(cmp.needsAlwaysOnServerAck()).toBe(false);
    });

    it('does not gate a single-tool server', async () => {
      // The gate prevents "I meant one tool and pinned thirty". One tool is
      // not that, and gating it rendered "I understand this pins all 1 tools".
      const cmp = makeComponent();
      await cmp.ngOnInit();
      makeServerForm(cmp, 1);
      cmp.form.patchValue({ toolEnablement: 'always_on' });
      expect(cmp.alwaysOnServerToolCount()).toBe(1);
      expect(cmp.needsAlwaysOnServerAck()).toBe(false);
    });

    it('still saves a single-tool server without an acknowledgement', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      makeServerForm(cmp, 1);
      cmp.form.patchValue({ toolEnablement: 'always_on' });
      await cmp.onSubmit();
      expect(adminToolService.createTool).toHaveBeenCalledTimes(1);
    });

    it('names how many tools would be pinned', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      makeServerForm(cmp, 3);
      cmp.form.patchValue({ toolEnablement: 'always_on' });
      expect(cmp.alwaysOnServerToolCount()).toBe(3);
      expect(cmp.needsAlwaysOnServerAck()).toBe(true);
    });

    it('blocks submit until the admin acknowledges', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      makeServerForm(cmp, 3);
      cmp.form.patchValue({ toolEnablement: 'always_on' });

      await cmp.onSubmit();
      expect(adminToolService.createTool).not.toHaveBeenCalled();

      cmp.form.patchValue({ acknowledgeAlwaysOnServer: true });
      await cmp.onSubmit();
      expect(adminToolService.createTool).toHaveBeenCalledTimes(1);
    });

    it('does not gate a single local tool', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      fillLocalTool(cmp);
      cmp.form.patchValue({ toolEnablement: 'always_on' });
      expect(cmp.needsAlwaysOnServerAck()).toBe(false);
    });
  });

  describe('per-tool always-on inside a server (§2.3)', () => {
    it('carries the flag onto each curated tool entry', async () => {
      const cmp = makeComponent();
      await cmp.ngOnInit();
      cmp.form.patchValue({
        toolId: 'weather_mcp',
        displayName: 'Weather',
        description: 'Weather server',
        protocol: 'mcp_external',
        mcpServerUrl: 'https://example.com/mcp',
      });
      cmp.addMcpTool();
      cmp.mcpToolsArray.at(0).patchValue({ name: 'get_forecast', alwaysOn: true });
      cmp.addMcpTool();
      cmp.mcpToolsArray.at(1).patchValue({ name: 'get_history' });

      await cmp.onSubmit();

      const tools = adminToolService.createTool.mock.calls[0][0].mcpConfig.tools;
      expect(tools).toEqual([
        { name: 'get_forecast', needsApproval: false, alwaysOn: true, description: null },
        { name: 'get_history', needsApproval: false, alwaysOn: false, description: null },
      ]);
    });
  });
});
