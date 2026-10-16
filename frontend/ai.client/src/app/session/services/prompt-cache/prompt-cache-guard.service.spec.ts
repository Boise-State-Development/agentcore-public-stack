import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import { CacheResetDialogData } from '../../../components/cache-reset-dialog/cache-reset-dialog.component';
import { LocalSettingsService } from '../../../services/local-settings.service';
import { ChatStateService } from '../chat/chat-state.service';
import { SessionService } from '../session/session.service';
import { MessageMapService } from '../session/message-map.service';
import {
  CACHE_WARM_MS,
  CacheResetChange,
  PromptCacheGuardService,
  estimateCacheResetUsd,
} from './prompt-cache-guard.service';

const NOW = Date.parse('2026-10-15T12:00:00Z');

function model(overrides: Partial<ManagedModel> = {}): ManagedModel {
  return {
    id: 'm',
    modelId: 'haiku',
    modelName: 'Haiku',
    provider: 'bedrock',
    providerName: 'Anthropic',
    inputModalities: ['TEXT'],
    outputModalities: ['TEXT'],
    maxInputTokens: 200000,
    maxOutputTokens: 8192,
    allowedAppRoles: [],
    availableToRoles: [],
    enabled: true,
    inputPricePerMillionTokens: 1.1,
    outputPricePerMillionTokens: 5.5,
    cacheWritePricePerMillionTokens: 1.375,
    cacheReadPricePerMillionTokens: 0.11,
    supportsCaching: true,
    isDefault: false,
    ...overrides,
  };
}

const HAIKU = model();
const SONNET = model({
  modelId: 'sonnet',
  modelName: 'Sonnet',
  inputPricePerMillionTokens: 3.3,
  cacheWritePricePerMillionTokens: 4.125,
  cacheReadPricePerMillionTokens: 0.33,
});
const SWITCH: CacheResetChange = { kind: 'model', from: HAIKU, to: SONNET };

function setup(options: {
  sessionId?: string;
  messages?: { createdAt?: string }[];
  loading?: boolean;
  turnEndedAt?: number | null;
  contextTokens?: number;
  confirmEnabled?: boolean;
  dialogResult?: { dontAskAgain: boolean } | undefined;
} = {}) {
  const open = vi.fn(() => ({ closed: of(options.dialogResult) }));
  const setConfirmCacheReset = vi.fn();
  const messages = signal(options.messages ?? [{ createdAt: new Date(NOW - 60_000).toISOString() }]);

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Dialog, useValue: { open } },
      {
        provide: ChatStateService,
        useValue: {
          isSessionLoading: () => options.loading ?? false,
          lastTurnEndedAt: () => options.turnEndedAt ?? null,
          contextTokensFor: () => options.contextTokens ?? 40_000,
        },
      },
      {
        provide: SessionService,
        useValue: { currentSession: signal({ sessionId: options.sessionId ?? 's1' }) },
      },
      { provide: MessageMapService, useValue: { getMessagesForSession: () => messages } },
      {
        provide: LocalSettingsService,
        useValue: { confirmCacheReset: signal(options.confirmEnabled ?? true), setConfirmCacheReset },
      },
    ],
  });
  return { guard: TestBed.inject(PromptCacheGuardService), open, setConfirmCacheReset };
}

function dialogData(open: ReturnType<typeof setup>['open']): CacheResetDialogData {
  return (open.mock.calls[0] as unknown as [unknown, { data: CacheResetDialogData }])[1].data;
}

describe('estimateCacheResetUsd', () => {
  it('prices a model switch as a write on the new model against a read on the old', () => {
    // 40K tokens: 40_000 × (4.125 − 0.11) / 1e6
    expect(estimateCacheResetUsd(SWITCH, 40_000)).toBeCloseTo(0.1606, 4);
  });

  it('prices an effort change as a write against a read on the same model', () => {
    const change: CacheResetChange = { kind: 'effort', model: SONNET, fromLabel: 'Low', toLabel: 'High' };
    expect(estimateCacheResetUsd(change, 40_000)).toBeCloseTo(0.1518, 4);
  });

  it('falls back to the standard multipliers when the row has no cache prices', () => {
    const bare = model({ cacheWritePricePerMillionTokens: null, cacheReadPricePerMillionTokens: null, inputPricePerMillionTokens: 2 });
    const change: CacheResetChange = { kind: 'effort', model: bare, fromLabel: 'Low', toLabel: 'High' };
    // 1M tokens × (2 × 1.25 − 2 × 0.1)
    expect(estimateCacheResetUsd(change, 1_000_000)).toBeCloseTo(2.3, 6);
  });

  it('is unknown when the conversation size is', () => {
    expect(estimateCacheResetUsd(SWITCH, 0)).toBeNull();
  });
});

describe('PromptCacheGuardService', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it('asks with the models, size and extra cost, and goes ahead on confirm', async () => {
    const { guard, open } = setup({ dialogResult: { dontAskAgain: false } });

    expect(await guard.confirm(SWITCH)).toBe(true);
    const data = dialogData(open);
    expect(data.title).toBe('Switch model?');
    expect(data.message).toContain('cached for Haiku');
    expect(data.message).toContain('Switching to Sonnet');
    expect(data.message).toContain('about 40K tokens');
    expect(data.message).toContain('$0.16 more');
  });

  it('words an effort change in effort levels', async () => {
    const { guard, open } = setup({ dialogResult: { dontAskAgain: false } });
    await guard.confirm({ kind: 'effort', model: SONNET, fromLabel: 'Low', toLabel: 'High' });

    const data = dialogData(open);
    expect(data.title).toBe('Change effort?');
    expect(data.message).toContain('cached at Low effort. Changing to High');
    expect(data.confirmText).toBe('Change effort');
  });

  it('stops the change when the user cancels', async () => {
    const { guard } = setup({ dialogResult: undefined });
    expect(await guard.confirm(SWITCH)).toBe(false);
  });

  it('remembers "Don\'t ask again"', async () => {
    const { guard, setConfirmCacheReset } = setup({ dialogResult: { dontAskAgain: true } });
    expect(await guard.confirm(SWITCH)).toBe(true);
    expect(setConfirmCacheReset).toHaveBeenCalledWith(false);
  });

  it('does not ask once the user opted out', async () => {
    const { guard, open } = setup({ confirmEnabled: false });
    expect(await guard.confirm(SWITCH)).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('does not ask in a conversation with no messages', async () => {
    const { guard, open } = setup({ messages: [] });
    expect(await guard.confirm(SWITCH)).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('does not ask once the cache has expired: the next message re-writes it anyway', async () => {
    const stale = new Date(NOW - CACHE_WARM_MS - 1000).toISOString();
    const { guard, open } = setup({ messages: [{ createdAt: stale }] });
    expect(await guard.confirm(SWITCH)).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('counts a long turn as warm from when it ended, not when it started', async () => {
    const started = new Date(NOW - CACHE_WARM_MS - 60_000).toISOString();
    const { guard, open } = setup({
      messages: [{ createdAt: started }],
      turnEndedAt: NOW - 30_000,
      dialogResult: { dontAskAgain: false },
    });
    await guard.confirm(SWITCH);
    expect(open).toHaveBeenCalled();
  });

  it('asks while a turn is streaming', async () => {
    const stale = new Date(NOW - CACHE_WARM_MS - 1000).toISOString();
    const { guard, open } = setup({ messages: [{ createdAt: stale }], loading: true, dialogResult: { dontAskAgain: false } });
    await guard.confirm(SWITCH);
    expect(open).toHaveBeenCalled();
  });

  it('does not ask when the re-write costs under a cent', async () => {
    const { guard, open } = setup({ contextTokens: 2_000 });
    expect(await guard.confirm(SWITCH)).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('does not ask when the current model has no cache to lose', async () => {
    const { guard, open } = setup();
    const change: CacheResetChange = { kind: 'model', from: model({ supportsCaching: false }), to: SONNET };
    expect(await guard.confirm(change)).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  it('still asks, without a figure, when the conversation size is unknown', async () => {
    const { guard, open } = setup({ contextTokens: 0, dialogResult: { dontAskAgain: false } });
    await guard.confirm(SWITCH);
    expect(dialogData(open).message).toContain('which uses more of your limit.');
    expect(dialogData(open).message).not.toContain('$');
  });
});
