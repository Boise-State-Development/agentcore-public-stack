import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { ModelService } from './model.service';
import { AuthService } from '../../../auth/auth.service';
import { ConfigService } from '../../../services/config.service';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import { signal } from '@angular/core';

describe('ModelService', () => {
  let service: ModelService;
  let httpMock: HttpTestingController;

  const mockModels: ManagedModel[] = [
    { id: 'm1', modelId: 'claude-haiku', modelName: 'Claude Haiku', provider: 'bedrock', providerName: 'Anthropic', inputModalities: ['TEXT'], outputModalities: ['TEXT'], maxInputTokens: 200000, maxOutputTokens: 4096, allowedAppRoles: [], availableToRoles: [], enabled: true, inputPricePerMillionTokens: 0.25, outputPricePerMillionTokens: 1.25, knowledgeCutoffDate: null, supportsCaching: true, isDefault: false },
    { id: 'm2', modelId: 'claude-sonnet', modelName: 'Claude Sonnet', provider: 'bedrock', providerName: 'Anthropic', inputModalities: ['TEXT'], outputModalities: ['TEXT'], maxInputTokens: 200000, maxOutputTokens: 4096, allowedAppRoles: [], availableToRoles: [], enabled: true, inputPricePerMillionTokens: 3, outputPricePerMillionTokens: 15, knowledgeCutoffDate: null, supportsCaching: true, isDefault: true },
  ];

  const mockResponse = { models: mockModels, totalCount: 2 };

  let sessionStore: Record<string, string> = {};

  async function setup() {
    sessionStore = {};
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn((k: string) => sessionStore[k] ?? null),
      setItem: vi.fn((k: string, v: string) => { sessionStore[k] = v; }),
      removeItem: vi.fn((k: string) => { delete sessionStore[k]; }),
    });

    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        ModelService,
        { provide: AuthService, useValue: { ensureAuthenticated: vi.fn().mockResolvedValue(undefined) } },
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });

    service = TestBed.inject(ModelService);
    httpMock = TestBed.inject(HttpTestingController);

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/models').flush(mockResponse);
    });
  }

  afterEach(() => {
    httpMock.match(() => true);
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  describe('loadModels', () => {
    beforeEach(setup);

    it('should load models and select default', () => {
      expect(service.availableModels()).toEqual(mockModels);
      expect(service.selectedModel().modelId).toBe('claude-sonnet'); // isDefault: true
      expect(service.modelsLoading()).toBe(false);
    });

    it('should handle error and fallback to DEFAULT_MODEL', async () => {
      const promise = service.loadModels();
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/models').error(new ProgressEvent('error'));
      });
      await promise;
      expect(service.selectedModel().id).toBe('system-default');
      expect(service.availableModels()).toEqual([]);
    });

    it('should restore from sessionStorage when no prior selection', async () => {
      // Reset to simulate fresh state: clear in-memory selection so sessionStorage is checked
      service['_selectedModel'].set(null);
      service['usingDefaultModel'].set(true);
      sessionStore['selectedModelId'] = 'claude-haiku';
      const promise = service.loadModels();
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/models').flush(mockResponse);
      });
      await promise;
      expect(service.selectedModel().modelId).toBe('claude-haiku');
    });
  });

  describe('setSelectedModel', () => {
    beforeEach(setup);

    it('should set model and persist to sessionStorage', () => {
      service.setSelectedModel(mockModels[0]);
      expect(service.selectedModel()).toEqual(mockModels[0]);
      expect(sessionStorage.setItem).toHaveBeenCalledWith('selectedModelId', 'claude-haiku');
    });
  });

  describe('setSelectedModelById', () => {
    beforeEach(setup);

    it('should find and select model', () => {
      expect(service.setSelectedModelById('claude-haiku')).toBe(true);
      expect(service.selectedModel().modelId).toBe('claude-haiku');
    });

    it('should return false for unknown model', () => {
      expect(service.setSelectedModelById('nonexistent')).toBe(false);
    });
  });

  describe('isUsingDefaultModel', () => {
    beforeEach(setup);

    it('should detect default model', () => {
      service.setSelectedModel(service.getDefaultModel());
      expect(service.isUsingDefaultModel()).toBe(true);
    });

    it('should detect non-default model', () => {
      service.setSelectedModel(mockModels[0]);
      expect(service.isUsingDefaultModel()).toBe(false);
    });
  });

  describe('getDefaultModel', () => {
    beforeEach(setup);

    it('should return system default', () => {
      expect(service.getDefaultModel().id).toBe('system-default');
    });
  });
});
