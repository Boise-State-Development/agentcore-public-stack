import { PrefixFingerprints, SessionCallRow } from '../models';

export type FingerprintKey = 'toolConfigHash' | 'systemPromptHash' | 'historyHash';

export const FINGERPRINT_KEYS: readonly FingerprintKey[] = [
  'toolConfigHash',
  'systemPromptHash',
  'historyHash',
];

export const FINGERPRINT_LABELS: Record<FingerprintKey, string> = {
  toolConfigHash: 'Tools',
  systemPromptHash: 'System',
  historyHash: 'History',
};

/** One call row annotated with fingerprint-diff results against the previous fingerprinted call. */
export interface AnatomyRow {
  call: SessionCallRow;
  index: number;
  /** Fingerprint hashes that flipped vs. the nearest previous call that has fingerprints. */
  changed: FingerprintKey[];
}

/**
 * Annotate chronological call rows with which prefix-fingerprint hashes
 * changed since the previous fingerprinted call — on a `miss_avoidable`
 * row, the flipped hash names the cache-buster.
 *
 * A hash is flagged only when both rows carry a value for it and the values
 * differ; rows predating the fingerprint feature (null fingerprints) are
 * skipped as comparison baselines.
 */
export function buildAnatomyRows(calls: SessionCallRow[]): AnatomyRow[] {
  let previous: PrefixFingerprints | undefined;

  return calls.map((call, index) => {
    const fingerprints = call.prefixFingerprints ?? undefined;
    const changed: FingerprintKey[] = [];

    if (fingerprints && previous) {
      for (const key of FINGERPRINT_KEYS) {
        const current = fingerprints[key];
        const before = previous[key];
        if (current != null && before != null && current !== before) {
          changed.push(key);
        }
      }
    }

    if (fingerprints) {
      previous = fingerprints;
    }

    return { call, index, changed };
  });
}

/** First 8 chars of a fingerprint hash, or an em dash when absent. */
export function truncateHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 8) : '—';
}
