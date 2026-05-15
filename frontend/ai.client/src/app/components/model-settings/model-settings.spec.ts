import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ElementRef, signal } from '@angular/core';
import { ModelService } from '../../session/services/model/model.service';
import { ToolService } from '../../services/tool/tool.service';
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
      tools: signal([]),
      enabledTools: signal([]),
      toolsByCategory: signal(new Map()),
      categories: signal([]),
      toggleTool: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: ModelService, useValue: mockModelService },
        { provide: ToolService, useValue: mockToolService },
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
});
