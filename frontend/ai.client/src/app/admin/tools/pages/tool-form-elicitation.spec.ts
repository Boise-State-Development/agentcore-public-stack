import { afterEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { ToolFormPage } from './tool-form.page';
import { AdminToolService } from '../services/admin-tool.service';
import { ConnectorsService } from '../../connectors/services/connectors.service';
import { FEATURES } from '../../../services/features';
import { environment } from '../../../../environments/environment';

/**
 * The per-server "Allow this server to ask users for input" toggle
 * (`elicitationEnabled`, docs/specs/mcp-elicitation.md §6). It is shown only
 * behind `features.mcpElicitation`, but the row's setting is saved either way,
 * so an edit made while the toggle is hidden never clears it.
 */
describe('ToolFormPage — MCP elicitation toggle', () => {
  let adminToolService: {
    createTool: ReturnType<typeof vi.fn>;
    updateTool: ReturnType<typeof vi.fn>;
    fetchTool: ReturnType<typeof vi.fn>;
    discoverMCPTools: ReturnType<typeof vi.fn>;
  };

  function make(mcpElicitation: boolean) {
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
        { provide: FEATURES, useValue: { ...environment.features, mcpElicitation } },
      ],
    });
    const fixture = TestBed.createComponent(ToolFormPage);
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    return fixture;
  }

  afterEach(() => TestBed.resetTestingModule());

  function fillServer(cmp: ToolFormPage): void {
    cmp.form.patchValue({
      toolId: 'github_issues',
      displayName: 'GitHub',
      description: 'GitHub server',
      protocol: 'mcp_external',
      mcpServerUrl: 'https://example.com/mcp',
    });
  }

  function toggle(root: HTMLElement): HTMLInputElement | null {
    return root.querySelector('input[formcontrolname="mcpElicitationEnabled"]');
  }

  it('shows the toggle on an external MCP server while the flag is on', async () => {
    const fixture = make(true);
    await fixture.componentInstance.ngOnInit();
    fillServer(fixture.componentInstance);
    fixture.detectChanges();

    const input = toggle(fixture.nativeElement);
    expect(input).not.toBeNull();
    expect(input!.getAttribute('aria-describedby')).toBe('mcp-elicitation-help');
  });

  it('hides it while the flag is off', async () => {
    const fixture = make(false);
    await fixture.componentInstance.ngOnInit();
    fillServer(fixture.componentInstance);
    fixture.detectChanges();

    expect(toggle(fixture.nativeElement)).toBeNull();
  });

  it('saves the choice on the server config', async () => {
    const fixture = make(true);
    const cmp = fixture.componentInstance;
    await cmp.ngOnInit();
    fillServer(cmp);
    cmp.form.patchValue({ mcpElicitationEnabled: true });

    await cmp.onSubmit();

    expect(adminToolService.createTool).toHaveBeenCalledWith(
      expect.objectContaining({
        mcpConfig: expect.objectContaining({ elicitationEnabled: true }),
      }),
    );
  });

  it('discovers with the setting, so tools gated on elicitation are listed', async () => {
    const fixture = make(true);
    const cmp = fixture.componentInstance;
    await cmp.ngOnInit();
    fillServer(cmp);
    cmp.form.patchValue({ mcpElicitationEnabled: true });

    await cmp.discoverMcpTools();

    expect(adminToolService.discoverMCPTools).toHaveBeenCalledWith(
      expect.objectContaining({ elicitationEnabled: true }),
    );
  });

  it('keeps a row\'s setting through an edit made with the toggle hidden', async () => {
    const fixture = make(false);
    const cmp = fixture.componentInstance;
    adminToolService.fetchTool.mockResolvedValue({
      toolId: 'github_issues',
      displayName: 'GitHub',
      description: 'GitHub server',
      category: 'utility',
      protocol: 'mcp_external',
      status: 'active',
      enabledByDefault: false,
      allowedAppRoles: [],
      mcpConfig: {
        serverUrl: 'https://example.com/mcp',
        transport: 'streamable-http',
        authType: 'none',
        tools: [],
        healthCheckEnabled: false,
        healthCheckIntervalSeconds: 300,
        elicitationEnabled: true,
      },
    });
    await cmp.loadTool('github_issues');

    expect(cmp.form.value.mcpElicitationEnabled).toBe(true);
  });
});
