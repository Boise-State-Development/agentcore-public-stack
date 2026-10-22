import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ModelService } from './model.service';
import { ConfigService } from '../../../services/config.service';
import { UserSettingsService } from '../../../services/user-settings.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';

/**
 * Model retirement in the chat picker (docs/specs/model-retirement.md §7):
 * a deprecated model can be kept but not newly chosen, and a selection on a
 * retired model follows it to its successor — out loud, once per tab.
 */
function model(modelId: string, extra: Partial<ManagedModel> = {}): ManagedModel {
  return {
    id: `uuid-${modelId}`,
    modelId,
    modelName: modelId.replace(/-/g, ' '),
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
    outputPricePerMillionTokens: 5,
    knowledgeCutoffDate: null,
    supportsCaching: true,
    isDefault: false,
    ...extra,
  };
}

const CATALOG: ManagedModel[] = [
  model('sonnet-5', { isDefault: true }),
  model('haiku-4-5'),
  model('sonnet-4-6', { status: 'deprecated', replacedBy: 'sonnet-5' }),
  model('opus-4-1', { status: 'retired', replacedBy: 'sonnet-4-6-old' }),
  model('sonnet-4-6-old', { status: 'retired', replacedBy: 'sonnet-5' }),
  model('claude-3', { status: 'retired' }),
];

describe('ModelService — retirement', () => {
  let httpMock: HttpTestingController;
  let sessionStore: Record<string, string>;
  let toast: { info: ReturnType<typeof vi.fn> };

  async function setup(opts: { session?: Record<string, string>; defaultModelId?: string | null } = {}) {
    sessionStore = { ...(opts.session ?? {}) };
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn((k: string) => sessionStore[k] ?? null),
      setItem: vi.fn((k: string, v: string) => {
        sessionStore[k] = v;
      }),
      removeItem: vi.fn((k: string) => {
        delete sessionStore[k];
      }),
    });
    toast = { info: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ModelService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        {
          provide: UserSettingsService,
          useValue: { getSettings: vi.fn().mockResolvedValue({ defaultModelId: opts.defaultModelId ?? null }) },
        },
        { provide: ToastService, useValue: toast },
      ],
    });

    const service = TestBed.inject(ModelService);
    httpMock = TestBed.inject(HttpTestingController);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/models').flush({ models: CATALOG, totalCount: CATALOG.length });
    });
    await vi.waitFor(() => expect(service.modelsLoading()).toBe(false));
    return service;
  }

  afterEach(() => {
    httpMock.match(() => true);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('never offers a retired model', async () => {
    const service = await setup();
    const ids = service.availableModels().map((m) => m.modelId);
    expect(ids).not.toContain('opus-4-1');
    expect(ids).not.toContain('claude-3');
  });

  it('hides a deprecated model from the picker unless it is the selection', async () => {
    const service = await setup();
    expect(service.featuredModels().map((m) => m.modelId)).not.toContain('sonnet-4-6');

    service.setSelectedModelById('sonnet-4-6');
    expect(service.featuredModels().map((m) => m.modelId)).toContain('sonnet-4-6');
  });

  it('still lists a deprecated model for surfaces that keep an existing choice', async () => {
    const service = await setup();
    expect(service.availableModels().map((m) => m.modelId)).toContain('sonnet-4-6');
  });

  it('follows a retired session selection through a chain to the live successor, and says so', async () => {
    const service = await setup({ session: { selectedModelId: 'opus-4-1' } });
    expect(service.selectedModel().modelId).toBe('sonnet-5');
    expect(toast.info).toHaveBeenCalledWith('opus 4 1 has been retired', 'Switched to sonnet 5.');
    // The successor is what the tab now remembers.
    expect(sessionStore['selectedModelId']).toBe('sonnet-5');
  });

  it('announces a redirect once per tab, not on every load', async () => {
    const service = await setup({
      session: { retiredModelNoticesShown: JSON.stringify(['opus-4-1']) },
      defaultModelId: 'opus-4-1',
    });
    expect(service.selectedModel().modelId).toBe('sonnet-5');
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('falls back normally when a retired model has no successor', async () => {
    const service = await setup({ session: { selectedModelId: 'claude-3' } });
    expect(service.selectedModel().modelId).toBe('sonnet-5'); // the admin default
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('shows an agent pin on a retired model as its successor, without a notice', async () => {
    const service = await setup();
    service.lockToAgentModel('sonnet-4-6-old');
    expect(service.selectedModel().modelId).toBe('sonnet-5');
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('exposes the successor and names of retired models for other surfaces', async () => {
    const service = await setup();
    expect(service.successorFor('opus-4-1')?.modelId).toBe('sonnet-5');
    expect(service.successorFor('sonnet-5')).toBeNull();
    expect(service.successorFor('claude-3')).toBeNull();
    expect(service.modelNameFor('claude-3')).toBe('claude 3');
  });
});
