import { describe, expect, it } from 'vitest';
import {
  ManagedModel,
  effortLevelLabel,
  resolveEffortControl,
} from './managed-model.model';

/**
 * `resolveEffortControl` decides whether the chat model picker offers an
 * Effort submenu at all. Its contract is "only offer what the backend will
 * actually honor" — `_merge_inference_params` keeps an enum override ONLY
 * when it's a member of the admin-declared `allowed` set, and pins `locked`
 * params to the admin default regardless of what the client sends. Every
 * case below is one of those backend behaviours, read back from the UI side.
 */
function model(params: Record<string, unknown> | null): ManagedModel {
  return {
    id: 'm1',
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
    outputPricePerMillionTokens: 1,
    knowledgeCutoffDate: null,
    supportsCaching: true,
    isDefault: false,
    supportedParams: params === null ? null : { params: params as never },
  };
}

describe('resolveEffortControl', () => {
  it('returns null when the model declares no params at all', () => {
    expect(resolveEffortControl(model(null))).toBeNull();
  });

  it('returns null for a null/undefined model', () => {
    expect(resolveEffortControl(null)).toBeNull();
    expect(resolveEffortControl(undefined)).toBeNull();
  });

  it('returns null when the model declares no effort param', () => {
    expect(resolveEffortControl(model({ temperature: { supported: true } }))).toBeNull();
  });

  it('returns null when effort is declared but unsupported', () => {
    const control = resolveEffortControl(
      model({ effort: { supported: false, allowed: ['low', 'high'] } }),
    );
    expect(control).toBeNull();
  });

  it('returns null when effort is supported but enumerates no levels', () => {
    // The backend's enum branch needs `allowed` to keep an override; without
    // it there is nothing safe to render.
    expect(resolveEffortControl(model({ effort: { supported: true } }))).toBeNull();
    expect(resolveEffortControl(model({ effort: { supported: true, allowed: [] } }))).toBeNull();
  });

  it('returns null when the admin locked the param', () => {
    // Locked params are pinned to the admin default server-side and user
    // overrides are dropped, so offering the choice would be a lie.
    const control = resolveEffortControl(
      model({ effort: { supported: true, locked: true, allowed: ['low', 'high'], default: 'low' } }),
    );
    expect(control).toBeNull();
  });

  it('resolves levels and default for a configured Bedrock effort param', () => {
    const control = resolveEffortControl(
      model({
        effort: { supported: true, allowed: ['low', 'medium', 'high'], default: 'medium' },
      }),
    );
    expect(control).toEqual({ key: 'effort', levels: ['low', 'medium', 'high'], defaultLevel: 'medium' });
  });

  it('reports no default when the admin declared none', () => {
    const control = resolveEffortControl(
      model({ effort: { supported: true, allowed: ['low', 'high'] } }),
    );
    expect(control?.defaultLevel).toBeNull();
  });

  it('ignores a default that is not one of the allowed levels', () => {
    // Mirrors the backend, which would fall through to the provider default
    // rather than send a value outside the declared set.
    const control = resolveEffortControl(
      model({ effort: { supported: true, allowed: ['low', 'high'], default: 'max' } }),
    );
    expect(control?.defaultLevel).toBeNull();
  });

  it('falls back to reasoning_effort on the OpenAI-compatible surfaces', () => {
    const control = resolveEffortControl(
      model({ reasoning_effort: { supported: true, allowed: ['low', 'high'], default: 'high' } }),
    );
    expect(control).toEqual({ key: 'reasoning_effort', levels: ['low', 'high'], defaultLevel: 'high' });
  });

  it('prefers effort over reasoning_effort when a model somehow declares both', () => {
    const control = resolveEffortControl(
      model({
        effort: { supported: true, allowed: ['low'] },
        reasoning_effort: { supported: true, allowed: ['high'] },
      }),
    );
    expect(control?.key).toBe('effort');
  });

  it('coerces non-string levels to strings', () => {
    const control = resolveEffortControl(
      model({ effort: { supported: true, allowed: [1, 2], default: 2 } }),
    );
    expect(control).toEqual({ key: 'effort', levels: ['1', '2'], defaultLevel: '2' });
  });
});

describe('effortLevelLabel', () => {
  it('title-cases a plain level', () => {
    expect(effortLevelLabel('low')).toBe('Low');
    expect(effortLevelLabel('medium')).toBe('Medium');
  });

  it('spells out the xhigh shorthand', () => {
    expect(effortLevelLabel('xhigh')).toBe('Extra high');
  });
});
