import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ModelService } from '../../session/services/model/model.service';
import { ToolService } from '../../services/tool/tool.service';
import { SkillService } from '../../services/skill/skill.service';
import { ManagedModel } from '../../admin/manage-models/models/managed-model.model';

describe('ModelSettings', () => {
  let mockModelService: any;
  let mockToolService: any;

  const mockModel: ManagedModel = {
    id: 'test-id',
    modelId: 'test-model',
    modelName: 'Test Model',
    provider: 'bedrock',
    providerName: 'Anthropic',
    inputModalities: ['TEXT'],
    outputModalities: ['TEXT'],
    maxInputTokens: 200000,
    maxOutputTokens: 4096,
    allowedAppRoles: [],
    availableToRoles: [],
    enabled: true,
    inputPricePerMillionTokens: 1,
    outputPricePerMillionTokens: 2,
    knowledgeCutoffDate: null,
    supportsCaching: true,
    isDefault: false,
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockModelService = {
      availableModels: signal([mockModel]),
      selectedModel: signal(mockModel),
      setSelectedModel: vi.fn(),
    };
    mockToolService = {
      tools: signal<any[]>([]),
      visibleTools: signal<any[]>([]),
      enabledTools: signal([]),
      toolsByCategory: signal(new Map()),
      categories: signal([]),
      isToolShownEnabled: (tool: any) => tool.isEnabled,
      toggleTool: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: ModelService, useValue: mockModelService },
        { provide: ToolService, useValue: mockToolService },
        // Mock SkillService so the component doesn't hold a real one (which
        // would fire /skills/ HTTP); its async error logs can otherwise land
        // during worker teardown and fail the run with an unhandled rejection.
        {
          provide: SkillService,
          useValue: {
            skills: signal([]),
            visibleSkills: signal([]),
            enabledSkillIds: signal([]),
            enabledCount: signal(0),
            hasSkills: signal(false),
            loading: signal(false),
            error: signal(null),
            agentLocked: signal(false),
            toggleSkill: vi.fn(),
          },
        },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { ModelSettings } = await import('./model-settings');
    return TestBed.runInInjectionContext(() => new ModelSettings());
  }

  it('owns neither the model picker nor the inference-param form', async () => {
    // Step 3 of docs/specs/customize-surface.md. The model section was a second
    // copy of `<app-model-dropdown />` (which lives in the chat input and carries
    // the Effort submenu with it), and the Advanced form's only remaining rows
    // were sampling knobs plus a duplicate of that same Effort control.
    //
    // Asserted on the API surface because this harness constructs the component
    // directly, with no fixture to query — but these are exactly the members a
    // reintroduction would bring back.
    const component = await createComponent();
    const gone = [
      'toggleModelDropdown',
      'selectModel',
      'isModelSelected',
      'onDropdownKeydown',
      'isModelDropdownOpen',
      'advancedRows',
      'hasAdvancedParams',
      'overriddenCount',
      'toggleAdvanced',
      'resetAllParams',
    ];
    const present = gone.filter(
      name => (component as unknown as Record<string, unknown>)[name] !== undefined,
    );
    expect(present).toEqual([]);
  });

  describe('tool detail pane', () => {
    const gmail = {
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
      isEnabled: false,
      serverTools: [
        { name: 'search_messages', enabled: true },
        { name: 'send_message', enabled: false },
      ],
    };

    beforeEach(() => {
      mockToolService.tools.set([gmail]);
    });

    it('opens the detail pane for a tool', async () => {
      const component = await createComponent();
      component.openToolDetail('gmail_employee');
      expect(component['detailTool']()?.toolId).toBe('gmail_employee');
    });

    it('keeps the last tool rendered after Back so the pane can slide out', async () => {
      const component = await createComponent();
      component.openToolDetail('gmail_employee');
      component.closeToolDetail();

      expect(component['detailTool']()).toBeNull();
      expect(component['lastDetailTool']()?.toolId).toBe('gmail_employee');
    });

    it('re-reads the tool from the service rather than holding a stale object', async () => {
      const component = await createComponent();
      component.openToolDetail('gmail_employee');

      // Toggling a sub-tool replaces the object in tools(); a captured
      // reference would keep rendering the old switch states.
      mockToolService.tools.set([
        { ...gmail, serverTools: [{ name: 'search_messages', enabled: false }] },
      ]);

      expect(component['detailTool']()?.serverTools).toEqual([
        { name: 'search_messages', enabled: false },
      ]);
    });

    it('escape backs out of the detail instead of closing the drawer', async () => {
      const component = await createComponent();
      component.openToolDetail('gmail_employee');

      const event = new KeyboardEvent('keydown', { key: 'Escape' });
      const prevented = vi.spyOn(event, 'preventDefault');
      component.onEscape(event);

      expect(component['detailTool']()).toBeNull();
      expect(prevented).toHaveBeenCalled();
    });

    it('leads the subtitle with the partial count when only some tools are on', async () => {
      const component = await createComponent();
      expect(component.subtitle(gmail as any)).toBe(
        '1 of 2 tools on · Securely connect to your inbox'
      );
    });

    it('leads the subtitle with the total when every tool is on', async () => {
      const component = await createComponent();
      const allOn = {
        ...gmail,
        serverTools: [
          { name: 'search_messages', enabled: true },
          { name: 'send_message', enabled: true },
        ],
      };
      expect(component.subtitle(allOn as any)).toBe(
        '2 tools · Securely connect to your inbox'
      );
    });

    it('uses the plain description for a tool with no sub-tools', async () => {
      const component = await createComponent();
      const calculator = { ...gmail, serverTools: [], description: 'Does sums' };
      expect(component.subtitle(calculator as any)).toBe('Does sums');
    });

    it('names which of a server’s tools matched the query', async () => {
      const component = await createComponent();
      component['toolQuery'].set('send_mess');
      expect(component.matchedSubTools(gmail as any)).toEqual(['send_message']);
    });

    it('stays quiet when the row’s own text already explains the match', async () => {
      const component = await createComponent();
      component['toolQuery'].set('gmail');
      expect(component.matchedSubTools(gmail as any)).toEqual([]);
    });

    it('builds a monogram from the display name', async () => {
      const component = await createComponent();
      expect(component.monogram(gmail as any)).toBe('GF');
      expect(component.monogram({ displayName: 'Excalidraw' } as any)).toBe('E');
      expect(component.monogram({ displayName: '1Password' } as any)).toBe('P');
      expect(component.monogram({ displayName: '///' } as any)).toBe('?');
    });
  });

  describe('search and grouping', () => {
    const tool = (over: Partial<any>) => ({
      toolId: 't',
      displayName: 'T',
      description: '',
      category: 'utility',
      icon: null,
      protocol: 'local',
      status: 'active',
      grantedBy: [],
      enabledByDefault: false,
      userEnabled: null,
      isEnabled: false,
      ...over,
    });

    const calculator = tool({ toolId: 'calculator', displayName: 'Calculator', category: 'utility' });
    const artifacts = tool({
      toolId: 'create_artifact',
      displayName: 'Artifacts',
      category: 'utility',
      isEnabled: true,
    });
    const github = tool({ toolId: 'github_repos', displayName: 'Github Repos', category: 'code' });
    const calendar = tool({
      toolId: 'google_calendar',
      displayName: 'Google Calendar',
      category: 'custom',
      protocol: 'mcp_external',
      serverTools: [
        { name: 'suggest_time', enabled: true },
        { name: 'list_events', enabled: true },
      ],
    });

    beforeEach(() => {
      mockToolService.tools.set([calculator, artifacts, github, calendar]);
      mockToolService.visibleTools = signal([calculator, artifacts, github, calendar]);
      mockToolService.isToolShownEnabled = (t: any) => t.isEnabled;
    });

    it('pins the enabled group above the categories', async () => {
      const component = await createComponent();
      const rows = component['toolRows']();
      expect(rows[0]).toMatchObject({ kind: 'header', id: 'enabled', count: 1 });
      expect(rows[1]).toMatchObject({ kind: 'tool', id: 'create_artifact' });
    });

    it('collapses categories by default so the list is headers, not 31 rows', async () => {
      const component = await createComponent();
      const rows = component['toolRows']();
      const toolIds = rows.filter((r: any) => r.kind === 'tool').map((r: any) => r.id);
      // Only the enabled tool has a row; the other three sit behind headers.
      expect(toolIds).toEqual(['create_artifact']);
      expect(rows.filter((r: any) => r.kind === 'header' && r.collapsible).map((r: any) => r.label))
        .toEqual(['Code & repositories', 'Custom', 'Utility']);
    });

    it('reveals a category’s tools when it is expanded', async () => {
      const component = await createComponent();
      component.toggleCategory('code');
      const ids = component['toolRows']().filter((r: any) => r.kind === 'tool').map((r: any) => r.id);
      expect(ids).toContain('github_repos');
    });

    it('flattens to one ungrouped run while searching', async () => {
      const component = await createComponent();
      component['toolQuery'].set('calc');
      const rows = component['toolRows']();
      expect(rows[0]).toMatchObject({ kind: 'header', id: 'results', collapsible: false, count: 1 });
      expect(rows[1]).toMatchObject({ kind: 'tool', id: 'calculator' });
    });

    it('matches a server through its own tool names', async () => {
      const component = await createComponent();
      component['toolQuery'].set('suggest_time');
      const ids = component['toolRows']().filter((r: any) => r.kind === 'tool').map((r: any) => r.id);
      expect(ids).toEqual(['google_calendar']);
    });

    it('emits no rows at all when nothing matches, so the empty state can show', async () => {
      const component = await createComponent();
      component['toolQuery'].set('zzzznope');
      // A bare "Results 0" header would make toolRows() non-empty and silently
      // suppress the empty state — that regression shipped once.
      expect(component['toolRows']()).toEqual([]);
    });

    it('falls back to the raw slug for a category the frontend does not know', async () => {
      const component = await createComponent();
      expect(component.categoryLabel('utility')).toBe('Utility');
      expect(component.categoryLabel('quantum_widgets')).toBe('quantum_widgets');
    });
  });
});
