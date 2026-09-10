import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ElementRef, signal } from '@angular/core';
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
      enabledTools: signal([]),
      toolsByCategory: signal(new Map()),
      categories: signal([]),
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
        { provide: ElementRef, useValue: { nativeElement: document.createElement('div') } },
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

  it('should initialize with closed dropdown state', async () => {
    const component = await createComponent();
    expect(component['isModelDropdownOpen']()).toBe(false);
    expect(component['focusedOptionIndex']()).toBe(-1);
  });

  it('should toggle model dropdown', async () => {
    const component = await createComponent();
    component.toggleModelDropdown();
    expect(component['isModelDropdownOpen']()).toBe(true);
    component.toggleModelDropdown();
    expect(component['isModelDropdownOpen']()).toBe(false);
  });

  it('should select model and close dropdown', async () => {
    const component = await createComponent();
    component.selectModel(mockModel);
    expect(mockModelService.setSelectedModel).toHaveBeenCalledWith(mockModel);
    expect(component['isModelDropdownOpen']()).toBe(false);
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

    it('escape leaves the detail alone while the model dropdown owns it', async () => {
      const component = await createComponent();
      component.openToolDetail('gmail_employee');
      component.toggleModelDropdown();

      component.onEscape(new KeyboardEvent('keydown', { key: 'Escape' }));

      expect(component['detailTool']()?.toolId).toBe('gmail_employee');
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

    it('builds a monogram from the display name', async () => {
      const component = await createComponent();
      expect(component.monogram(gmail as any)).toBe('GF');
      expect(component.monogram({ displayName: 'Excalidraw' } as any)).toBe('E');
      expect(component.monogram({ displayName: '1Password' } as any)).toBe('P');
      expect(component.monogram({ displayName: '///' } as any)).toBe('?');
    });
  });
});
