import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ToolDetailComponent } from './tool-detail.component';
import { Tool, ToolService } from '../../../services/tool/tool.service';

/**
 * Rendered rather than instantiated: the point of this component is what the
 * pane shows for a given tool, and half of that ("no tabs for a plain tool",
 * "discover instead of a list") only exists in the template.
 */
describe('ToolDetailComponent', () => {
  let mockToolService: any;
  let fixture: ComponentFixture<ToolDetailComponent>;

  const mcpServer: Tool = {
    toolId: 'gmail_employee',
    displayName: 'Gmail for Employees',
    description: 'Securely connect to your inbox',
    category: 'utility',
    icon: null,
    protocol: 'mcp_external',
    status: 'active',
    grantedBy: ['employee'],
    enabledByDefault: false,
    userEnabled: null,
    isEnabled: true,
    serverTools: [
      { name: 'search_messages', description: 'Search the mailbox.', enabled: true },
      { name: 'send_message', description: 'Send an email.', enabled: false },
    ],
  };

  const localTool: Tool = {
    toolId: 'calculator',
    displayName: 'Calculator',
    description: 'Evaluate arithmetic.',
    category: 'utility',
    icon: null,
    protocol: 'local',
    status: 'active',
    grantedBy: [],
    enabledByDefault: false,
    userEnabled: null,
    isEnabled: false,
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockToolService = {
      tools: signal<Tool[]>([]),
      agentLocked: signal(false),
      isToolShownEnabled: (tool: Tool) => tool.isEnabled,
      isSubToolShownEnabled: (_tool: Tool, sub: { enabled: boolean }) => sub.enabled,
      toggleTool: vi.fn().mockResolvedValue(undefined),
      toggleServerTool: vi.fn().mockResolvedValue(undefined),
      discoverServerTools: vi.fn().mockResolvedValue(undefined),
    };

    TestBed.configureTestingModule({
      imports: [ToolDetailComponent],
      providers: [{ provide: ToolService, useValue: mockToolService }],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function render(tool: Tool): ComponentFixture<ToolDetailComponent> {
    fixture = TestBed.createComponent(ToolDetailComponent);
    fixture.componentRef.setInput('tool', tool);
    fixture.detectChanges();
    return fixture;
  }

  const text = () => fixture.nativeElement.textContent as string;

  it('lists an MCP server’s tools on the Tools tab', () => {
    render(mcpServer);
    expect(text()).toContain('search_messages');
    expect(text()).toContain('send_message');
    expect(text()).toContain('Search the mailbox.');
  });

  it('shows a tab strip with the tool count for an MCP server', () => {
    render(mcpServer);
    const tabs = fixture.nativeElement.querySelectorAll('[role="tab"]');
    expect(tabs.length).toBe(2);
    expect(tabs[0].textContent).toContain('Tools');
    expect(tabs[0].textContent).toContain('2');
  });

  it('gives a plain tool no tab strip and opens it on About', () => {
    render(localTool);
    expect(fixture.nativeElement.querySelectorAll('[role="tab"]').length).toBe(0);
    expect(fixture.nativeElement.querySelector('#tool-detail-panel-about')).toBeTruthy();
    expect(text()).toContain('calculator');
  });

  it('offers discovery instead of an empty list when a server has no known tools', () => {
    render({ ...mcpServer, serverTools: [] });
    expect(text()).toContain('hasn’t been asked what it offers yet');
    const button = fixture.nativeElement.querySelector('button[disabled]');
    expect(button).toBeNull();
  });

  it('calls the service when a sub-tool is toggled', () => {
    render(mcpServer);
    const switches = fixture.nativeElement.querySelectorAll(
      '[role="switch"][aria-labelledby^="subtool-"]'
    );
    switches[1].click();
    expect(mockToolService.toggleServerTool).toHaveBeenCalledWith(
      'gmail_employee',
      'send_message'
    );
  });

  it('surfaces a discovery failure rather than failing silently', async () => {
    mockToolService.discoverServerTools.mockRejectedValue(new Error('502'));
    render({ ...mcpServer, serverTools: [] });

    await fixture.componentInstance['discover']();
    fixture.detectChanges();

    expect(text()).toContain('Could not list this server');
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeTruthy();
  });

  it('replaces the switch with a lock when the toolset is agent-bound', () => {
    mockToolService.agentLocked.set(true);
    render(mcpServer);

    // Removed, not disabled: a disabled toggle invites a click and then
    // refuses it. The server-level switch must be absent entirely.
    const stateSwitch = fixture.nativeElement.querySelector(
      '[role="switch"][aria-labelledby="tool-detail-state"]'
    );
    expect(stateSwitch).toBeNull();
    expect(text()).toContain('Fixed by this agent');
  });

  it('shows catalog facts on the About tab', () => {
    render(mcpServer);
    fixture.componentInstance['tab'].set('about');
    fixture.detectChanges();

    expect(text()).toContain('mcp_external');
    expect(text()).toContain('employee');
    expect(text()).toContain('gmail_employee');
  });

  it('resets to the default tab when a different tool is shown', () => {
    render(mcpServer);
    fixture.componentInstance['tab'].set('about');
    fixture.detectChanges();

    fixture.componentRef.setInput('tool', { ...mcpServer, toolId: 'canvas_faculty' });
    fixture.detectChanges();

    expect(fixture.componentInstance['tab']()).toBe('tools');
  });

  it('moves between tabs with the arrow keys', () => {
    render(mcpServer);
    const event = new KeyboardEvent('keydown', { key: 'ArrowRight' });
    fixture.componentInstance['onTabKeydown'](event);
    expect(fixture.componentInstance['tab']()).toBe('about');

    fixture.componentInstance['onTabKeydown'](new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    expect(fixture.componentInstance['tab']()).toBe('tools');
  });
});
