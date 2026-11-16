import { describe, it, expect, vi, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { ModelDataNoticeService } from './model-data-notice.service';
import { UserSettingsService } from '../../../services/user-settings.service';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';

function model(overrides: Partial<ManagedModel> = {}): ManagedModel {
  return {
    id: 'f',
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
    enabled: true,
    inputPricePerMillionTokens: 8,
    outputPricePerMillionTokens: 40,
    supportsCaching: true,
    isDefault: false,
    effectiveDataHandling: 'provider_review',
    dataHandlingNoticeText: 'Kept for up to 30 days.',
    dataHandlingNoticeDigest: 'digest-1',
    ...overrides,
  };
}

describe('ModelDataNoticeService', () => {
  let open: ReturnType<typeof vi.fn>;
  let answer: boolean;
  let settings: { getSettings: ReturnType<typeof vi.fn>; acknowledgeModelNotice: ReturnType<typeof vi.fn> };

  function setup(acknowledgements: Record<string, { acknowledgedAt: string; noticeDigest: string | null }> | null = null) {
    answer = true;
    open = vi.fn(() => ({ closed: of(answer) }));
    settings = {
      getSettings: vi.fn(async () => ({ defaultModelId: null, modelNoticeAcknowledgements: acknowledgements })),
      acknowledgeModelNotice: vi.fn(async () => ({})),
    };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: Dialog, useValue: { open } },
        { provide: UserSettingsService, useValue: settings },
      ],
    });
    return TestBed.inject(ModelDataNoticeService);
  }

  beforeEach(() => TestBed.resetTestingModule());

  it('lets a standard model through without asking', async () => {
    const service = setup();
    expect(await service.confirmBeforeChoosing(model({ effectiveDataHandling: 'standard' }))).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('shows the notice the first time and records the confirmation', async () => {
    const service = setup();
    expect(await service.confirmBeforeChoosing(model())).toBe(true);
    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0][1].data.message).toBe('Kept for up to 30 days.');
    expect(settings.acknowledgeModelNotice).toHaveBeenCalledWith('global.anthropic.claude-fable-5-2');
  });

  it('does not ask again for the same wording', async () => {
    const service = setup({ 'global.anthropic.claude-fable-5-2': { acknowledgedAt: 'x', noticeDigest: 'digest-1' } });
    expect(await service.confirmBeforeChoosing(model())).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('asks again once the wording changes', async () => {
    const service = setup({ 'global.anthropic.claude-fable-5-2': { acknowledgedAt: 'x', noticeDigest: 'old' } });
    await service.confirmBeforeChoosing(model());
    expect(open).toHaveBeenCalledTimes(1);
  });

  it('declining records nothing and keeps the current model', async () => {
    const service = setup();
    answer = false;
    expect(await service.confirmBeforeChoosing(model())).toBe(false);
    expect(settings.acknowledgeModelNotice).not.toHaveBeenCalled();
  });

  it('a failed save still honours the confirmation', async () => {
    const service = setup();
    settings.acknowledgeModelNotice.mockRejectedValueOnce(new Error('503'));
    expect(await service.confirmBeforeChoosing(model())).toBe(true);
  });
});
