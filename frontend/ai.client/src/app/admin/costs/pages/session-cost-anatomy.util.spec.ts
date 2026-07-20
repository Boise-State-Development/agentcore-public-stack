import { describe, it, expect } from 'vitest';
import { buildAnatomyRows, truncateHash } from './session-cost-anatomy.util';
import { SessionCallRow } from '../models';

function makeCall(overrides: Partial<SessionCallRow> = {}): SessionCallRow {
  return {
    timestamp: '2026-07-19T10:00:00Z',
    inputTokens: 100,
    outputTokens: 50,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    cost: 0.01,
    wastedUsd: 0,
    ...overrides,
  };
}

describe('buildAnatomyRows', () => {
  it('never flags the first call', () => {
    const rows = buildAnatomyRows([
      makeCall({
        prefixFingerprints: { toolConfigHash: 'aaa', systemPromptHash: 'bbb', historyHash: 'ccc' },
      }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].changed).toEqual([]);
  });

  it('flags exactly the hash that flipped between consecutive calls', () => {
    const rows = buildAnatomyRows([
      makeCall({
        prefixFingerprints: { toolConfigHash: 'aaa', systemPromptHash: 'bbb', historyHash: 'ccc' },
      }),
      makeCall({
        prefixFingerprints: { toolConfigHash: 'XXX', systemPromptHash: 'bbb', historyHash: 'ccc' },
      }),
    ]);
    expect(rows[1].changed).toEqual(['toolConfigHash']);
  });

  it('flags multiple flipped hashes', () => {
    const rows = buildAnatomyRows([
      makeCall({
        prefixFingerprints: { toolConfigHash: 'aaa', systemPromptHash: 'bbb', historyHash: 'ccc' },
      }),
      makeCall({
        prefixFingerprints: { toolConfigHash: 'aaa', systemPromptHash: 'YYY', historyHash: 'ZZZ' },
      }),
    ]);
    expect(rows[1].changed).toEqual(['systemPromptHash', 'historyHash']);
  });

  it('skips rows without fingerprints as comparison baselines', () => {
    const rows = buildAnatomyRows([
      makeCall({
        prefixFingerprints: { toolConfigHash: 'aaa', systemPromptHash: 'bbb', historyHash: 'ccc' },
      }),
      makeCall({ prefixFingerprints: null }),
      makeCall({
        prefixFingerprints: { toolConfigHash: 'XXX', systemPromptHash: 'bbb', historyHash: 'ccc' },
      }),
    ]);
    // Row 1 has nothing to diff; row 2 diffs against row 0, not the null row.
    expect(rows[1].changed).toEqual([]);
    expect(rows[2].changed).toEqual(['toolConfigHash']);
  });

  it('does not flag a hash when either side is missing', () => {
    const rows = buildAnatomyRows([
      makeCall({ prefixFingerprints: { toolConfigHash: 'aaa', historyHash: 'ccc' } }),
      makeCall({
        prefixFingerprints: { toolConfigHash: 'aaa', systemPromptHash: 'now-present', historyHash: 'ccc' },
      }),
    ]);
    expect(rows[1].changed).toEqual([]);
  });

  it('does not flag identical fingerprints', () => {
    const fp = { toolConfigHash: 'aaa', systemPromptHash: 'bbb', historyHash: 'ccc' };
    const rows = buildAnatomyRows([
      makeCall({ prefixFingerprints: fp }),
      makeCall({ prefixFingerprints: { ...fp } }),
    ]);
    expect(rows[1].changed).toEqual([]);
  });
});

describe('truncateHash', () => {
  it('truncates to the first 8 chars', () => {
    expect(truncateHash('0123456789abcdef')).toBe('01234567');
  });

  it('returns an em dash for missing hashes', () => {
    expect(truncateHash(null)).toBe('—');
    expect(truncateHash(undefined)).toBe('—');
    expect(truncateHash('')).toBe('—');
  });
});
