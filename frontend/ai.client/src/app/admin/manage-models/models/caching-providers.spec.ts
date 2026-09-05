import { describe, it, expect } from 'vitest';
import {
  AVAILABLE_PROVIDERS,
  CACHING_DEFAULT_PROVIDERS,
  CACHING_FORCED_PROVIDERS,
  defaultSupportsCaching,
  supportsCachingForProvider,
} from './managed-model.model';

/**
 * `supportsCaching` policy.
 *
 * The form always posts a value, so the backend's own provider default never
 * sees `None` from the UI and can never apply — which is why this policy has to
 * exist on the frontend at all. It is mirrored on the backend and pinned in
 * step by `backend/tests/shared/test_caching_provider_contract.py`.
 */
describe('supportsCaching policy', () => {
  describe('defaultSupportsCaching', () => {
    it.each(['bedrock', 'bedrock-responses'] as const)('defaults on for %s', provider => {
      expect(defaultSupportsCaching(provider)).toBe(true);
    });

    it.each(['mantle', 'openai', 'gemini'] as const)('defaults off for %s', provider => {
      expect(defaultSupportsCaching(provider)).toBe(false);
    });
  });

  describe('supportsCachingForProvider', () => {
    it('honours an explicit choice where caching is optional', () => {
      expect(supportsCachingForProvider('bedrock', false)).toBe(false);
      expect(supportsCachingForProvider('mantle', true)).toBe(true);
    });

    it('falls back to the provider default when nothing was chosen', () => {
      expect(supportsCachingForProvider('bedrock', null)).toBe(true);
      expect(supportsCachingForProvider('mantle', undefined)).toBe(false);
    });

    it.each([true, false, null, undefined])(
      'forces bedrock-responses on regardless of the chosen value (%s)',
      chosen => {
        // Caching there is implicit and server-side — nothing we send turns it
        // off. A stored false only clears the cache rates, which prices cached
        // tokens at $0.00 while the provider bills them in full. On a warm
        // conversation nearly every input token is a cache read.
        expect(supportsCachingForProvider('bedrock-responses', chosen)).toBe(true);
      },
    );
  });

  describe('provider lists', () => {
    it('only names providers that actually exist', () => {
      for (const p of [...CACHING_DEFAULT_PROVIDERS, ...CACHING_FORCED_PROVIDERS]) {
        expect(AVAILABLE_PROVIDERS).toContain(p);
      }
    });

    it('forces only providers that also default on', () => {
      // A forced provider that was not also a default would be contradictory.
      for (const p of CACHING_FORCED_PROVIDERS) {
        expect(CACHING_DEFAULT_PROVIDERS).toContain(p);
      }
    });
  });
});
