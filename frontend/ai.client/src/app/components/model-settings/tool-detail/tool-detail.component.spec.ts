import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ToolDetailComponent } from './tool-detail.component';
import { Tool, ToolService } from '../../../services/tool/tool.service';
import { ToolCapabilityService } from '../../../services/tool-capability/tool-capability.service';

/**
 * Rendered rather than instantiated: the point of this component is what the
 * pane shows for a given tool, and half of that ("no tabs for a plain tool",
 * "discover instead of a list") only exists in the template.
 */
describe('ToolDetailComponent', () => {
  let mockToolService: any;
  let mockCapabilityService: any;
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

    mockCapabilityService = {
      snapshots: {} as Record<string, any>,
      loading: new Set<string>(),
      ensure: vi.fn().mockResolvedValue(undefined),
      capabilitiesFor(toolId: string) {
        return this.snapshots[toolId] ?? null;
      },
      isLoading(toolId: string) {
        return this.loading.has(toolId);
      },
      hasFailed: () => false,
      invalidate: vi.fn(),
    };

    TestBed.configureTestingModule({
      imports: [ToolDetailComponent],
      providers: [
        // ConnectorsService is reached through this component's tree and fetches
        // in its constructor; without a testing backend that is a real socket.
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ToolService, useValue: mockToolService },
        { provide: ToolCapabilityService, useValue: mockCapabilityService },
      ],
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

  it('shows Tools, Prompts, Resources and About for an external MCP server', () => {
    render(mcpServer);
    const tabs = [...fixture.nativeElement.querySelectorAll('[role="tab"]')];
    expect(tabs.map((t: any) => t.textContent.replace(/\s+/g, ' ').trim())).toEqual([
      'Tools 2',
      'Prompts 0',
      'Resources 0',
      'About',
    ]);
  });

  it('offers no prompts or resources tab for a Gateway target', () => {
    // A Gateway target exposes tools only — there is no server to ask.
    render({ ...mcpServer, protocol: 'mcp' });
    const labels = [...fixture.nativeElement.querySelectorAll('[role="tab"]')].map(
      (t: any) => t.textContent.trim()
    );
    expect(labels.some((l: string) => l.startsWith('Prompts'))).toBe(false);
    expect(labels.some((l: string) => l.startsWith('Resources'))).toBe(false);
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

  it('moves between tabs with the arrow keys and wraps', () => {
    render(mcpServer);
    const right = () =>
      fixture.componentInstance['onTabKeydown'](
        new KeyboardEvent('keydown', { key: 'ArrowRight' })
      );

    right();
    expect(fixture.componentInstance['tab']()).toBe('prompts');
    right();
    expect(fixture.componentInstance['tab']()).toBe('resources');
    right();
    expect(fixture.componentInstance['tab']()).toBe('about');
    right();
    expect(fixture.componentInstance['tab']()).toBe('tools');
  });

  describe('prompts and resources', () => {
    const snapshot = (over: Partial<any> = {}) => ({
      toolId: 'gmail_employee',
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

    it('loads the snapshot for the tool being shown', () => {
      render(mcpServer);
      expect(mockCapabilityService.ensure).toHaveBeenCalledWith('gmail_employee');
    });

    it('lists prompts with their arguments', () => {
      mockCapabilityService.snapshots['gmail_employee'] = snapshot({
        supportsPrompts: true,
        prompts: [
          {
            name: 'summarise_unread',
            title: null,
            description: 'Group unread mail by thread.',
            arguments: ['since'],
          },
        ],
      });
      render(mcpServer);
      fixture.componentInstance['tab'].set('prompts');
      fixture.detectChanges();

      expect(text()).toContain('summarise_unread');
      expect(text()).toContain('Group unread mail by thread.');
      expect(text()).toContain('since');
    });

    it('separates "offers no prompts" from "never asked"', () => {
      // The server answered prompts/list with "method not found".
      mockCapabilityService.snapshots['gmail_employee'] = snapshot({
        supportsPrompts: false,
      });
      render(mcpServer);
      fixture.componentInstance['tab'].set('prompts');
      fixture.detectChanges();
      expect(text()).toContain('doesn’t offer prompts');

      // Nobody has run a discovery against it at all.
      mockCapabilityService.snapshots['gmail_employee'] = snapshot({
        discoveredAt: null,
      });
      fixture.componentRef.setInput('tool', { ...mcpServer });
      fixture.componentInstance['tab'].set('prompts');
      fixture.detectChanges();
      expect(text()).toContain('Nobody has asked this server');
    });

    it('marks a resource template as a pattern rather than a readable URI', () => {
      mockCapabilityService.snapshots['gmail_employee'] = snapshot({
        supportsResources: true,
        resources: [
          {
            uri: 'canvas://courses/{course_id}/syllabus',
            name: null,
            description: null,
            mimeType: null,
            uriTemplate: true,
          },
        ],
      });
      render(mcpServer);
      fixture.componentInstance['tab'].set('resources');
      fixture.detectChanges();

      expect(text()).toContain('canvas://courses/{course_id}/syllabus');
      expect(text()).toContain('template');
    });

    it('says when the stored list was capped', () => {
      mockCapabilityService.snapshots['gmail_employee'] = snapshot({
        supportsResources: true,
        truncated: true,
        resources: [
          { uri: 'x://1', name: null, description: null, mimeType: null, uriTemplate: false },
        ],
      });
      render(mcpServer);
      fixture.componentInstance['tab'].set('resources');
      fixture.detectChanges();
      expect(text()).toContain('capped');
    });
  });
});
