import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { Subject } from 'rxjs';
import { ModelCatalogPage } from './model-catalog.page';
import { ManagedModelsService } from './services/managed-models.service';
import { CuratedModelPrefillService } from './services/curated-model-prefill.service';
import { AddCuratedModelDialogComponent } from './components/add-curated-model-dialog.component';
import {
  CURATED_BEDROCK_MODELS,
  CURATED_BEDROCK_RESPONSES_MODELS,
  CURATED_MANTLE_MODELS,
} from './models/curated-models';

function createMockManagedModelsService(overrides: Partial<{
  isModelAdded: (modelId: string) => boolean;
  createModel: ReturnType<typeof vi.fn>;
}> = {}) {
  return {
    isModelAdded: overrides.isModelAdded ?? (() => false),
    createModel: overrides.createModel ?? vi.fn().mockResolvedValue({ id: 'created' }),
  };
}

function createMockPrefillService() {
  return {
    set: vi.fn(),
    consume: vi.fn().mockReturnValue(null),
  };
}

/**
 * Mock CDK Dialog: each call to `open()` returns a dialogRef whose `closed`
 * observable can be resolved imperatively in the test via the returned
 * `resolve()` helper.
 */
function createMockDialog() {
  const opened: Array<{ component: unknown; data: unknown; closed: Subject<unknown> }> = [];
  const open = vi.fn((component: unknown, config: { data: unknown }) => {
    const closed = new Subject<unknown>();
    opened.push({ component, data: config.data, closed });
    return { closed };
  });
  const lastOpened = () => opened[opened.length - 1];
  const resolveLast = (value: unknown) => {
    const last = lastOpened();
    last.closed.next(value);
    last.closed.complete();
  };
  return { open, opened, lastOpened, resolveLast };
}

describe('ModelCatalogPage', () => {
  let mockService: ReturnType<typeof createMockManagedModelsService>;
  let mockPrefill: ReturnType<typeof createMockPrefillService>;
  let mockDialog: ReturnType<typeof createMockDialog>;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockService = createMockManagedModelsService();
    mockPrefill = createMockPrefillService();
    mockDialog = createMockDialog();
    routerNavigate = vi.fn().mockResolvedValue(true);

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: ManagedModelsService, useValue: mockService },
        { provide: CuratedModelPrefillService, useValue: mockPrefill },
        { provide: Dialog, useValue: mockDialog },
      ],
    });
    TestBed.overrideComponent(ModelCatalogPage, {
      set: { template: '<div></div>' },
    });
    TestBed.overrideProvider(Router, { useValue: { navigate: routerNavigate } });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(ModelCatalogPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('defaults to the Bedrock tab and renders the curated entries', () => {
    const page = createComponent();
    expect(page.activeTab()).toBe('bedrock');
    expect(page.visibleModels().map(m => m.key)).toEqual(
      CURATED_BEDROCK_MODELS.map(m => m.key),
    );
  });

  it('shows an empty list when switching to OpenAI or Gemini (Coming soon state)', () => {
    const page = createComponent();
    page.selectTab('openai');
    expect(page.visibleModels()).toEqual([]);
    page.selectTab('gemini');
    expect(page.visibleModels()).toEqual([]);
  });

  it('Preview & customize hands the template to the prefill service and navigates to the form', () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    page.previewCuratedModel(target);

    expect(mockPrefill.set).toHaveBeenCalledWith(target.template);
    expect(routerNavigate).toHaveBeenCalledWith(['/admin/manage-models/new']);
    expect(mockService.createModel).not.toHaveBeenCalled();
  });

  it('addCuratedModel opens the role-picker dialog with the model in data', () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    page.addCuratedModel(target);

    expect(mockDialog.open).toHaveBeenCalledTimes(1);
    expect(mockDialog.opened[0].component).toBe(AddCuratedModelDialogComponent);
    expect(mockDialog.opened[0].data).toEqual({ model: target });
  });

  it('POSTs the template with selected roles when the dialog resolves with role IDs', async () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    const pending = page.addCuratedModel(target);
    mockDialog.resolveLast(['role-user', 'role-admin']);
    await pending;

    expect(mockService.createModel).toHaveBeenCalledWith({
      ...target.template,
      allowedAppRoles: ['role-user', 'role-admin'],
    });
    expect(routerNavigate).toHaveBeenCalledWith(['/admin/manage-models']);
    expect(page.addingKey()).toBeNull();
  });

  it('does not POST when the dialog is cancelled', async () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    const pending = page.addCuratedModel(target);
    mockDialog.resolveLast(undefined);
    await pending;

    expect(mockService.createModel).not.toHaveBeenCalled();
    expect(routerNavigate).not.toHaveBeenCalled();
  });

  it('marks a model as already added when the service reports it exists', () => {
    const existingId = CURATED_BEDROCK_MODELS[0].template.modelId;
    mockService = createMockManagedModelsService({
      isModelAdded: (id) => id === existingId,
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    expect(page.isAlreadyAdded(existingId)).toBe(true);
    expect(page.isAlreadyAdded(CURATED_BEDROCK_MODELS[1].template.modelId)).toBe(false);
  });

  it('does not open the dialog when the model is already in the managed list', () => {
    const target = CURATED_BEDROCK_MODELS[0];
    mockService = createMockManagedModelsService({
      isModelAdded: (id) => id === target.template.modelId,
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    page.addCuratedModel(target);
    expect(mockDialog.open).not.toHaveBeenCalled();
  });

  it('surfaces backend error.detail inline on the card without navigating', async () => {
    const failure = Object.assign(new Error('http failed'), {
      error: { detail: 'Model ID already in use' },
    });
    mockService = createMockManagedModelsService({
      createModel: vi.fn().mockRejectedValue(failure),
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    const pending = page.addCuratedModel(target);
    mockDialog.resolveLast(['role-user']);
    await pending;

    expect(page.errorFor(target.key)).toBe('Model ID already in use');
    expect(routerNavigate).not.toHaveBeenCalled();
    expect(page.addingKey()).toBeNull();
  });

  it('renders curated Mantle cards (with vetted API surface) on the Mantle tab', () => {
    const page = createComponent();
    page.selectTab('mantle');

    const keys = page.visibleModels().map(m => m.key);
    expect(keys).toEqual(CURATED_MANTLE_MODELS.map(m => m.key));

    // The Qwen coder speaks Chat Completions.
    const qwen = page.visibleModels().find(m => m.key === 'qwen3-coder-30b');
    expect(qwen?.template.apiMode).toBe('chat');
    // Mantle models never cache (model-bound to Claude/Nova).
    expect(qwen?.template.supportsCaching).toBe(false);
  });

  it('ignores a second addCuratedModel while a create is in flight', async () => {
    let resolveCreate: (value: unknown) => void = () => {};
    const createPromise = new Promise(res => { resolveCreate = res; });
    mockService = createMockManagedModelsService({
      createModel: vi.fn().mockReturnValue(createPromise),
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    const [first, second] = CURATED_BEDROCK_MODELS;

    const inFlight = page.addCuratedModel(first);
    mockDialog.resolveLast(['role-user']);
    // Wait for the dialog promise + into the createModel call before issuing the second.
    await Promise.resolve();
    await Promise.resolve();

    page.addCuratedModel(second);
    expect(mockDialog.open).toHaveBeenCalledTimes(1);

    resolveCreate({ id: 'created' });
    await inFlight;
    expect(page.addingKey()).toBeNull();
  });

  // These guard a mismatch that shipped and stayed live for months: every
  // curated Claude template declared the `global.*` rates while its `modelId`
  // named a `us.*` (Regional/CRIS) inference profile, which prices ~10% higher.
  // Nothing failed — the numbers were merely wrong, everywhere downstream.
  describe('curated Bedrock pricing', () => {
    // The CRIS tier a model id resolves to drives its rate card, so the two
    // must agree on every list that declares a tier — not just Bedrock's.
    const tieredModels = [...CURATED_BEDROCK_MODELS, ...CURATED_BEDROCK_RESPONSES_MODELS];

    it('declares a pricingTier that matches the tier its modelId names', () => {
      for (const model of tieredModels) {
        const expected = model.template.modelId.startsWith('global.') ? 'global' : 'regional';
        expect(`${model.key}:${model.pricingTier}`).toBe(`${model.key}:${expected}`);
      }
    });

    it('derives cache rates from base input at Bedrock\'s published multipliers', () => {
      for (const model of tieredModels) {
        const t = model.template;
        if (!t.supportsCaching) continue;
        const input = t.inputPricePerMillionTokens;
        expect(t.cacheWritePricePerMillionTokens).toBeCloseTo(input * 1.25, 6);
        expect(t.cacheReadPricePerMillionTokens).toBeCloseTo(input * 0.1, 6);
      }
    });
  });

  describe('curated bedrock-responses (GPT-5.6) entries', () => {
    it('renders them on their own tab', () => {
      const page = createComponent();
      page.selectTab('bedrock-responses');

      expect(page.visibleModels().map(m => m.key)).toEqual(
        CURATED_BEDROCK_RESPONSES_MODELS.map(m => m.key),
      );
      expect(CURATED_BEDROCK_RESPONSES_MODELS.length).toBeGreaterThan(0);
    });

    it('never ships supportsCaching false — the provider forces it true', () => {
      // `false` here is not a preference but a false statement: these models
      // cache implicitly server-side and it cannot be turned off. Its only
      // effect would be to clear the cache rates, pricing cached tokens at
      // $0.00 while AWS bills them in full.
      for (const model of CURATED_BEDROCK_RESPONSES_MODELS) {
        expect(`${model.key}:${model.template.supportsCaching}`).toBe(`${model.key}:true`);
      }
    });

    it('pins maxInputTokens to the 272K short-context boundary', () => {
      // Load-bearing pricing, not just a cap: above 272K these models bill
      // input at 2x and output at 1.5x, and a CuratedModel holds one flat rate
      // per bucket. Raising this silently opens the second price card.
      for (const model of CURATED_BEDROCK_RESPONSES_MODELS) {
        expect(`${model.key}:${model.template.maxInputTokens}`).toBe(`${model.key}:272000`);
      }
    });

    it('routes over the Responses API, which is the only surface that caches', () => {
      for (const model of CURATED_BEDROCK_RESPONSES_MODELS) {
        expect(`${model.key}:${model.template.apiMode}`).toBe(`${model.key}:responses`);
        expect(`${model.key}:${model.template.provider}`).toBe(`${model.key}:bedrock-responses`);
      }
    });

    it('declares no supportedParams rather than an invented one', () => {
      // AWS publishes no parameter table for GPT-5.6. A declared spec flips the
      // #915 guard from permissive to restrictive, so a guessed one would
      // silently block params the model actually accepts.
      for (const model of CURATED_BEDROCK_RESPONSES_MODELS) {
        expect(model.template.supportedParams ?? null).toBeNull();
      }
    });
  });

  it('curates GPT-5.4 on Mantle with caching on and no write fee', () => {
    // Its model card publishes a cache-read rate with an em dash for cache
    // write. Inheriting mantleDefaults()' supportsCaching:false priced its
    // cached tokens at $0.00 while AWS billed them — the bug that had to be
    // fixed by hand in prod.
    const gpt54 = CURATED_MANTLE_MODELS.find(m => m.key === 'gpt-5-4');

    expect(gpt54?.template.supportsCaching).toBe(true);
    expect(gpt54?.template.cacheReadPricePerMillionTokens).toBeCloseTo(0.275, 6);
    expect(gpt54?.template.cacheWritePricePerMillionTokens).toBe(0);
  });
});
