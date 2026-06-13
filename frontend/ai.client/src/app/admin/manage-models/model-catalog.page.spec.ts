import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { Subject } from 'rxjs';
import { ModelCatalogPage } from './model-catalog.page';
import { ManagedModelsService } from './services/managed-models.service';
import { CuratedModelPrefillService } from './services/curated-model-prefill.service';
import { AddCuratedModelDialogComponent } from './components/add-curated-model-dialog.component';
import { CURATED_BEDROCK_MODELS, CURATED_MANTLE_MODELS } from './models/curated-models';

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

  it('renders curated Mantle cards (with vetted endpoint paths) on the Mantle tab', () => {
    const page = createComponent();
    page.selectTab('mantle');

    const keys = page.visibleModels().map(m => m.key);
    expect(keys).toEqual(CURATED_MANTLE_MODELS.map(m => m.key));

    // Gemma 4 must carry the /openai/v1 path; the Qwen coder the default /v1.
    const gemma = page.visibleModels().find(m => m.key === 'gemma-4-31b');
    const qwen = page.visibleModels().find(m => m.key === 'qwen3-coder-30b');
    expect(gemma?.template.mantleEndpointPath).toBe('/openai/v1');
    expect(qwen?.template.mantleEndpointPath).toBe('/v1');
    // Mantle models never cache (model-bound to Claude/Nova).
    expect(gemma?.template.supportsCaching).toBe(false);
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
});
