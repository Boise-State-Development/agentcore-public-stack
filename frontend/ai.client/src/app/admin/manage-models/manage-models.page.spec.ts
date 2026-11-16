import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { ManageModelsPage } from './manage-models.page';
import { ManagedModelsService } from './services/managed-models.service';
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
    effectiveDataHandling: 'provider_review',
    dataHandlingNoticeText: 'Kept for up to 30 days.',
    ...overrides,
  };
}

describe('ManageModelsPage: enabling a model kept for provider review', () => {
  let updateModel: ReturnType<typeof vi.fn>;
  let open: ReturnType<typeof vi.fn>;
  let answer: boolean;

  function setup(models: ManagedModel[]) {
    updateModel = vi.fn(async () => models[0]);
    answer = true;
    open = vi.fn(() => ({ closed: of(answer) }));
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: Dialog, useValue: { open } },
        {
          provide: ManagedModelsService,
          useValue: {
            getManagedModels: () => models,
            modelsResource: { isLoading: signal(false), error: signal(null), reload: vi.fn() },
            updateModel,
            reorderModels: vi.fn(),
          },
        },
      ],
    });
    const fixture = TestBed.createComponent(ManageModelsPage);
    fixture.detectChanges();
    return fixture;
  }

  function toggle(fixture: ReturnType<typeof setup>, name: string): HTMLButtonElement {
    return fixture.nativeElement.querySelector(`button[aria-label="Enable ${name}"], button[aria-label="Disable ${name}"]`);
  }

  beforeEach(() => TestBed.resetTestingModule());

  it('badges the row', () => {
    const fixture = setup([model()]);
    expect(fixture.nativeElement.textContent).toContain('Kept for review');
  });

  it('asks first, then sends the acknowledgement', async () => {
    const fixture = setup([model()]);
    toggle(fixture, 'Claude Fable 5.2').click();
    await vi.waitFor(() => expect(updateModel).toHaveBeenCalled());
    expect(open).toHaveBeenCalledTimes(1);
    expect(updateModel).toHaveBeenCalledWith('row-1', { enabled: true, acknowledgeDataHandling: true });
  });

  it('changes nothing when the admin cancels', async () => {
    const fixture = setup([model()]);
    answer = false;
    toggle(fixture, 'Claude Fable 5.2').click();
    await vi.waitFor(() => expect(open).toHaveBeenCalled());
    await Promise.resolve();
    expect(updateModel).not.toHaveBeenCalled();
  });

  it('disabling, or a standard model, asks nothing', async () => {
    const fixture = setup([
      model({ enabled: true }),
      model({ id: 'row-2', modelId: 'us.anthropic.claude-opus-5-5', modelName: 'Opus', effectiveDataHandling: 'standard' }),
    ]);
    toggle(fixture, 'Claude Fable 5.2').click();
    toggle(fixture, 'Opus').click();
    await vi.waitFor(() => expect(updateModel).toHaveBeenCalledTimes(2));
    expect(open).not.toHaveBeenCalled();
    expect(updateModel).toHaveBeenCalledWith('row-1', { enabled: false });
    expect(updateModel).toHaveBeenCalledWith('row-2', { enabled: true });
  });
});
