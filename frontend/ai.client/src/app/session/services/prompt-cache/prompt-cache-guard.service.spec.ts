import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import { CacheResetDialogData } from '../../../components/cache-reset-dialog/cache-reset-dialog.component';
import { LocalSettingsService } from '../../../services/local-settings.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ModelService } from '../model/model.service';
import { SessionService } from '../session/session.service';
import { MessageMapService } from '../session/message-map.service';
import {
  DEFAULT_CACHE_TTL_MS,
  RESPONSES_CACHE_TTL_MS,
  cacheTtlMsFor,
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
const LUNA = model({
  modelId: 'luna',
  modelName: 'GPT-6 Luna',
  provider: 'bedrock-responses',
  inputPricePerMillionTokens: 0.5,
  cacheWritePricePerMillionTokens: null,
  cacheReadPricePerMillionTokens: 0.05,
});
const FROM_LUNA: CacheResetChange = { kind: 'model', from: LUNA, to: SONNET };

function setup(options: {
  sessionId?: string;
  messages?: { createdAt?: string; role?: string; metadata?: Record<string, unknown> }[];
  loading?: boolean;
  turnEndedAt?: number | null;
  contextTokens?: number;
  confirmEnabled?: boolean;
  dialogResult?: { dontAskAgain: boolean } | undefined;
  /** The session's persisted `lastModel`, as loaded from the list. */
  lastModel?: string;
  /** The effort level each model would send with, by model id (default: none). */
  efforts?: Record<string, string>;
  /** The id of the model the picker starts on (default: haiku). */
  selectedModelId?: string;
} = {}) {
  const open = vi.fn(() => ({ closed: of(options.dialogResult) }));
  const setConfirmCacheReset = vi.fn();
  const messages = signal(options.messages ?? [{ createdAt: new Date(NOW - 60_000).toISOString() }]);
  let turnEndedAt = options.turnEndedAt ?? null;
  /** The model the picker shows; `selectedEffort` is that model's level. */
  const picker = { modelId: options.selectedModelId ?? 'haiku' };

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Dialog, useValue: { open } },
      {
        provide: ChatStateService,
        useValue: {
          isSessionLoading: () => options.loading ?? false,
          lastTurnEndedAt: () => turnEndedAt,
          contextTokensFor: () => options.contextTokens ?? 40_000,
        },
      },
      {
        provide: ModelService,
        useValue: {
          selectedEffort: () => options.efforts?.[picker.modelId] ?? null,
          effortFor: (m: ManagedModel) => options.efforts?.[m.modelId] ?? null,
        },
      },
      {
        provide: SessionService,
        useValue: {
          currentSession: signal({
            sessionId: options.sessionId ?? 's1',
            preferences: options.lastModel ? { lastModel: options.lastModel } : undefined,
          }),
        },
      },
      { provide: MessageMapService, useValue: { getMessagesForSession: () => messages } },
      {
        provide: LocalSettingsService,
        useValue: { confirmCacheReset: signal(options.confirmEnabled ?? true), setConfirmCacheReset },
      },
    ],
  });
  return {
    guard: TestBed.inject(PromptCacheGuardService),
    open,
    setConfirmCacheReset,
    picker,
    /** A turn finishing in this tab at `at` (epoch ms). */
    endTurn: (at: number) => (turnEndedAt = at),
  };
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
    const change: CacheResetChange = { kind: 'effort', model: SONNET, fromLabel: 'Low', toLabel: 'High', toLevel: 'high' };
    expect(estimateCacheResetUsd(change, 40_000)).toBeCloseTo(0.1518, 4);
  });

  it('falls back to the standard multipliers when the row has no cache prices', () => {
    const bare = model({ cacheWritePricePerMillionTokens: null, cacheReadPricePerMillionTokens: null, inputPricePerMillionTokens: 2 });
    const change: CacheResetChange = { kind: 'effort', model: bare, fromLabel: 'Low', toLabel: 'High', toLevel: 'high' };
    // 1M tokens × (2 × 1.25 − 2 × 0.1)
    expect(estimateCacheResetUsd(change, 1_000_000)).toBeCloseTo(2.3, 6);
  });

  it('is unknown when the conversation size is', () => {
    expect(estimateCacheResetUsd(SWITCH, 0)).toBeNull();
  });
});

describe('cacheTtlMsFor', () => {
  it('gives the Responses API its 30-minute cache', () => {
    expect(cacheTtlMsFor({ provider: 'bedrock-responses' })).toBe(RESPONSES_CACHE_TTL_MS);
    expect(RESPONSES_CACHE_TTL_MS).toBe(30 * 60 * 1000);
  });

  it('gives Bedrock Converse its 5-minute cache', () => {
    expect(cacheTtlMsFor({ provider: 'bedrock' })).toBe(DEFAULT_CACHE_TTL_MS);
    expect(DEFAULT_CACHE_TTL_MS).toBe(5 * 60 * 1000);
  });

  it('does not guess for mantle, whose retention is undocumented (as the backend)', () => {
    expect(cacheTtlMsFor({ provider: 'mantle' })).toBe(DEFAULT_CACHE_TTL_MS);
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
    await guard.confirm({ kind: 'effort', model: SONNET, fromLabel: 'Low', toLabel: 'High', toLevel: 'high' });

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
    const stale = new Date(NOW - DEFAULT_CACHE_TTL_MS - 1000).toISOString();
    const { guard, open } = setup({ messages: [{ createdAt: stale }] });
    expect(await guard.confirm(SWITCH)).toBe(true);
    expect(open).not.toHaveBeenCalled();
  });

  describe('the window follows the provider of the model that wrote the cache', () => {
    const minutesAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();

    it('still asks 20 minutes after a GPT reply: the Responses cache lives 30 minutes', async () => {
      const { guard, open } = setup({ messages: [{ createdAt: minutesAgo(20) }], dialogResult: { dontAskAgain: false } });
      await guard.confirm(FROM_LUNA);
      expect(open).toHaveBeenCalled();
    });

    it('stops asking once the Responses cache has expired', async () => {
      const { guard, open } = setup({ messages: [{ createdAt: minutesAgo(31) }] });
      expect(await guard.confirm(FROM_LUNA)).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });

    it('does not stretch a Claude conversation to 30 minutes', async () => {
      const { guard, open } = setup({ messages: [{ createdAt: minutesAgo(20) }] });
      expect(await guard.confirm(SWITCH)).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });
  });

  it('counts a long turn as warm from when it ended, not when it started', async () => {
    const started = new Date(NOW - DEFAULT_CACHE_TTL_MS - 60_000).toISOString();
    const { guard, open } = setup({
      messages: [{ createdAt: started }],
      turnEndedAt: NOW - 30_000,
      dialogResult: { dontAskAgain: false },
    });
    await guard.confirm(SWITCH);
    expect(open).toHaveBeenCalled();
  });

  it('asks while a turn is streaming', async () => {
    const stale = new Date(NOW - DEFAULT_CACHE_TTL_MS - 1000).toISOString();
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
  describe('weighs the change against what the cache holds, not the picker', () => {
    const SONNET_HIGH: CacheResetChange = { kind: 'effort', model: SONNET, fromLabel: 'Medium', toLabel: 'High', toLevel: 'high' };

    it('asks when leaving the model the last message was sent with', async () => {
      const { guard, open } = setup({ dialogResult: { dontAskAgain: false } });
      guard.recordSend('s1', 'haiku', null);
      await guard.confirm(SWITCH);
      expect(open).toHaveBeenCalled();
    });

    it('does not ask on top of a switch picked but not sent: that already pays the re-write', async () => {
      const { guard, open } = setup();
      guard.recordSend('s1', 'haiku', null);
      // Picked Sonnet without sending, now picking Opus, or changing Sonnet's effort.
      const third: CacheResetChange = { kind: 'model', from: SONNET, to: model({ modelId: 'opus', modelName: 'Opus' }) };
      expect(await guard.confirm(third)).toBe(true);
      expect(await guard.confirm(SONNET_HIGH)).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });

    it('does not ask when going back to the model the cache holds', async () => {
      const { guard, open } = setup();
      guard.recordSend('s1', 'haiku', null);
      expect(await guard.confirm({ kind: 'model', from: SONNET, to: HAIKU })).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });

    it('does not ask when going back to the effort the cache holds', async () => {
      const { guard, open } = setup({ efforts: { sonnet: 'low' }, selectedModelId: 'sonnet' });
      guard.recordSend('s1', 'sonnet', 'high');
      const back: CacheResetChange = { kind: 'effort', model: SONNET, fromLabel: 'Low', toLabel: 'High', toLevel: 'high' };
      expect(await guard.confirm(back)).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });

    it('asks when changing effort on the model the last message used', async () => {
      const { guard, open } = setup({
        efforts: { sonnet: 'medium' },
        selectedModelId: 'sonnet',
        dialogResult: { dontAskAgain: false },
      });
      guard.recordSend('s1', 'sonnet', 'medium');
      await guard.confirm(SONNET_HIGH);
      expect(open).toHaveBeenCalled();
    });

    it("falls back to the session's lastModel for a conversation opened from the list", async () => {
      const { guard, open } = setup({ lastModel: 'haiku' });
      expect(await guard.confirm({ kind: 'model', from: SONNET, to: HAIKU })).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });
  });
  describe('a model keeps its own cache entry after a switch away from it', () => {
    const at = (minutesAgo: number) => NOW - minutesAgo * 60_000;
    const BACK_TO_HAIKU: CacheResetChange = { kind: 'model', from: LUNA, to: HAIKU };

    /** Haiku replied, then one GPT turn: the conversation seen on dev, 2026-10-16. */
    function haikuThenLuna(guard: PromptCacheGuardService, endTurn: (at: number) => void, haikuEnded: number) {
      vi.setSystemTime(haikuEnded - 30_000);
      guard.recordSend('s1', 'haiku', null);
      endTurn(haikuEnded);
      vi.setSystemTime(at(1.5));
      guard.recordSend('s1', 'luna', null);
      endTurn(at(1));
      vi.setSystemTime(NOW);
    }

    it('does not ask when switching back to a model still warm within its own lifetime', async () => {
      const { guard, open, endTurn } = setup();
      haikuThenLuna(guard, endTurn, at(4));
      expect(await guard.confirm(BACK_TO_HAIKU)).toBe(true);
      expect(open).not.toHaveBeenCalled();
    });

    it("asks when switching back after that model's own lifetime", async () => {
      const { guard, open, endTurn } = setup({ dialogResult: { dontAskAgain: false } });
      // Six minutes is inside GPT's 30 but past Haiku's 5.
      haikuThenLuna(guard, endTurn, at(6));
      expect(await guard.confirm(BACK_TO_HAIKU)).toBe(true);
      expect(dialogData(open).message).toContain('cached for GPT-6 Luna. Switching to Haiku');
    });

    it('asks when leaving the warm model it went back to for a third', async () => {
      const { guard, open, endTurn, picker } = setup({ dialogResult: { dontAskAgain: false } });
      haikuThenLuna(guard, endTurn, at(4));
      await guard.confirm(BACK_TO_HAIKU);
      picker.modelId = 'haiku';
      await guard.confirm(SWITCH);
      expect(dialogData(open).message).toContain('cached for Haiku. Switching to Sonnet');
    });

    describe('effort is part of the entry', () => {
      const SONNET_LOW_TO_HIGH: CacheResetChange = { kind: 'effort', model: SONNET, fromLabel: 'Low', toLabel: 'High', toLevel: 'high' };

      function sonnetThenLuna(guard: PromptCacheGuardService, endTurn: (at: number) => void) {
        vi.setSystemTime(at(3.5));
        guard.recordSend('s1', 'sonnet', 'low');
        endTurn(at(3));
        vi.setSystemTime(at(1.5));
        guard.recordSend('s1', 'luna', null);
        endTurn(at(1));
        vi.setSystemTime(NOW);
      }

      it('asks on an effort change once back on the warm model', async () => {
        const { guard, open, endTurn, picker } = setup({
          efforts: { sonnet: 'low' },
          selectedModelId: 'luna',
          dialogResult: { dontAskAgain: false },
        });
        sonnetThenLuna(guard, endTurn);
        expect(await guard.confirm({ kind: 'model', from: LUNA, to: SONNET })).toBe(true);
        expect(open).not.toHaveBeenCalled();

        picker.modelId = 'sonnet';
        await guard.confirm(SONNET_LOW_TO_HIGH);
        expect(dialogData(open).title).toBe('Change effort?');
      });

      it('asks when switching back to the model at a different effort than its entry', async () => {
        const { guard, open, endTurn } = setup({
          efforts: { sonnet: 'high' },
          selectedModelId: 'luna',
          dialogResult: { dontAskAgain: false },
        });
        sonnetThenLuna(guard, endTurn);
        await guard.confirm({ kind: 'model', from: LUNA, to: SONNET });
        expect(open).toHaveBeenCalled();
      });
    });

    describe('in a conversation opened from the list', () => {
      const reply = (modelId: string, minutesAgo: number) => ({
        role: 'assistant',
        createdAt: new Date(at(minutesAgo)).toISOString(),
        metadata: { modelInfo: { modelId } },
      });

      it("reads each reply's model from the history", async () => {
        const { guard, open } = setup({ messages: [reply('haiku', 4), reply('luna', 1)] });
        expect(await guard.confirm(BACK_TO_HAIKU)).toBe(true);
        expect(open).not.toHaveBeenCalled();
      });

      it("still asks once that model's lifetime has passed", async () => {
        const { guard, open } = setup({
          messages: [reply('haiku', 6), reply('luna', 1)],
          dialogResult: { dontAskAgain: false },
        });
        await guard.confirm(BACK_TO_HAIKU);
        expect(open).toHaveBeenCalled();
      });

      it('asks for a model with an effort control, since the history does not record effort', async () => {
        const opus = model({
          modelId: 'opus',
          modelName: 'Opus',
          supportedParams: { params: { effort: { supported: true, allowed: ['low', 'high'], default: 'low' } } as never },
        });
        const { guard, open } = setup({
          messages: [reply('opus', 3), reply('luna', 1)],
          efforts: { opus: 'low' },
          dialogResult: { dontAskAgain: false },
        });
        await guard.confirm({ kind: 'model', from: LUNA, to: opus });
        expect(open).toHaveBeenCalled();
      });
    });
  });
});
