import { describe, it, expect, vi, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { ModelFormPage } from './model-form.page';
import { ManagedModelsService } from './services/managed-models.service';
import { CuratedModelPrefillService } from './services/curated-model-prefill.service';
import { RoleOptionsService } from '../roles/services/role-options.service';
import { ManagedModel } from './models/managed-model.model';

function model(overrides: Partial<ManagedModel> = {}): ManagedModel {
  return {
    id: 'row-1',
    modelId: 'global.anthropic.claude-fable-5-2',
    modelName: 'Claude Fable 5.2',
    provider: 'bedrock',
    providerName: 'Anthropic',
    inputModalities: ['TEXT'],
    outputModalities: ['TEXT'],
    maxInputTokens: 1_000_000,
    maxOutputTokens: 128_000,
    allowedAppRoles: [],
    availableToRoles: [],
    enabled: false,
    inputPricePerMillionTokens: 8,
    outputPricePerMillionTokens: 40,
    supportsCaching: true,
    isDefault: false,
    dataHandling: 'provider_review',
    effectiveDataHandling: 'provider_review',
    dataHandlingNotice: null,
    dataHandlingNoticeText: 'Kept for up to 30 days.',
    ...overrides,
  };
}

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const lastDialog = () => {
  const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
  return containers[containers.length - 1];
};

async function setup(row: ManagedModel) {
  const updateModel = vi.fn(async (_id: string, _data: Partial<ManagedModel> & { acknowledgeDataHandling?: boolean }) => row);
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([{ path: '**', children: [] }]),
      { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: row.id }), queryParams: {} } } },
      {
        provide: ManagedModelsService,
        useValue: {
          getModel: vi.fn(async () => row),
          getManagedModels: () => [],
          updateModel,
          createModel: vi.fn(),
          fetchMantleModels: vi.fn(async () => ({ models: [] })),
        },
      },
      { provide: CuratedModelPrefillService, useValue: { consume: () => null, set: vi.fn() } },
      {
        provide: RoleOptionsService,
        useValue: {
          rolesResource: { isLoading: signal(false), error: signal(null), value: signal([]), reload: vi.fn() },
          getEnabledRoles: () => [],
        },
      },
    ],
  });
  const fixture = TestBed.createComponent(ModelFormPage);
  fixture.detectChanges();
  const form = fixture.componentInstance as unknown as {
    modelForm: ModelFormPage['modelForm'];
    onSubmit(): Promise<void>;
  };
  await vi.waitFor(() => expect(form.modelForm.controls.modelId.value).toBe(row.modelId));
  fixture.detectChanges();
  await tick();
  fixture.detectChanges();
  return { fixture, form, updateModel };
}

function footerButton(label: string): HTMLButtonElement {
  return Array.from(lastDialog().querySelectorAll<HTMLButtonElement>('[dialogFooter] button')).find(
    b => b.textContent?.trim() === label,
  )!;
}

describe('ModelFormPage: data handling', () => {
  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('locks a covered model to provider review, since the backend stores it that way', async () => {
    const { fixture } = await setup(model());
    const select = (fixture.nativeElement as HTMLElement).querySelector<HTMLSelectElement>('#dataHandling')!;
    expect(select.value).toBe('provider_review');
    expect(select.disabled).toBe(true);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Kept for up to 30 days.');
  });

  it('enabling it asks first, then saves with the acknowledgement', async () => {
    const { form, updateModel } = await setup(model());
    form.modelForm.controls.enabled.setValue(true);
    const saving = form.onSubmit();
    await tick();
    expect(lastDialog().textContent).toContain('Make Claude Fable 5.2 available?');
    footerButton('Make available').click();
    await saving;
    expect(updateModel).toHaveBeenCalledWith(
      'row-1',
      expect.objectContaining({ enabled: true, dataHandling: 'provider_review', acknowledgeDataHandling: true }),
    );
  });

  it('cancelling saves nothing', async () => {
    const { form, updateModel } = await setup(model());
    form.modelForm.controls.enabled.setValue(true);
    const saving = form.onSubmit();
    await tick();
    footerButton('Cancel').click();
    await saving;
    expect(updateModel).not.toHaveBeenCalled();
  });

  it('an edit to a model that is already enabled asks nothing', async () => {
    const { form, updateModel } = await setup(model({ enabled: true }));
    form.modelForm.controls.modelName.setValue('Fable');
    await form.onSubmit();
    expect(document.querySelectorAll('.cdk-dialog-container').length).toBe(0);
    expect(updateModel.mock.calls[0][1]).not.toHaveProperty('acknowledgeDataHandling');
  });

  it('a standard model can be marked, and enabling it then asks', async () => {
    const opus = model({
      modelId: 'us.anthropic.claude-opus-5-5',
      modelName: 'Claude Opus 5.5',
      dataHandling: 'standard',
      effectiveDataHandling: 'standard',
      dataHandlingNoticeText: null,
      enabled: true,
    });
    const { fixture, form, updateModel } = await setup(opus);
    const select = (fixture.nativeElement as HTMLElement).querySelector<HTMLSelectElement>('#dataHandling')!;
    expect(select.disabled).toBe(false);
    form.modelForm.controls.dataHandling.setValue('provider_review');
    const saving = form.onSubmit();
    await tick();
    footerButton('Make available').click();
    await saving;
    expect(updateModel).toHaveBeenCalledWith('row-1', expect.objectContaining({ acknowledgeDataHandling: true }));
  });
});
