import { describe, it, expect, beforeEach } from 'vitest';
import {
  McpAppCardStateService,
  McpAppCard,
} from './mcp-app-card-state.service';

function card(over: Partial<McpAppCard> = {}): McpAppCard {
  return {
    cardId: 'c1',
    toolUseId: 'tu1',
    toolName: 'widget_tool',
    arguments: {},
    content: [{ type: 'text', text: 'ok' }],
    isError: false,
    createdAt: '2026-01-01T00:00:00Z',
    producedByMessageIndex: null,
    ...over,
  };
}

describe('McpAppCardStateService', () => {
  let svc: McpAppCardStateService;

  beforeEach(() => {
    svc = new McpAppCardStateService();
  });

  it('seeds cards and sorts oldest-first', () => {
    svc.seedFromHydration([
      card({ cardId: 'b', createdAt: '2026-01-02T00:00:00Z' }),
      card({ cardId: 'a', createdAt: '2026-01-01T00:00:00Z' }),
    ]);
    expect(svc.hasCards()).toBe(true);
    expect(svc.cards().map((c) => c.cardId)).toEqual(['a', 'b']);
  });

  it('seedFromHydration is non-clobbering by cardId', () => {
    svc.seedFromHydration([card({ cardId: 'a', toolName: 'first' })]);
    // A later (e.g. slower) response must not overwrite an existing card.
    svc.seedFromHydration([card({ cardId: 'a', toolName: 'second' })]);
    expect(svc.cards()).toHaveLength(1);
    expect(svc.cards()[0].toolName).toBe('first');
  });

  it('empty seed is a no-op', () => {
    svc.seedFromHydration([]);
    expect(svc.hasCards()).toBe(false);
    expect(svc.cards()).toEqual([]);
  });

  it('reset() clears all cards', () => {
    svc.seedFromHydration([card()]);
    svc.reset();
    expect(svc.hasCards()).toBe(false);
    expect(svc.cards()).toEqual([]);
  });

  describe('grouping by originating tool use', () => {
    it('groups cards under the App frame that ran them, oldest-first', () => {
      svc.seedFromHydration([
        card({ cardId: 'b', toolUseId: 'tu1', createdAt: '2026-01-02T00:00:00Z' }),
        card({ cardId: 'c', toolUseId: 'tu2', createdAt: '2026-01-03T00:00:00Z' }),
        card({ cardId: 'a', toolUseId: 'tu1', createdAt: '2026-01-01T00:00:00Z' }),
      ]);
      expect(svc.cardsFor('tu1').map((c) => c.cardId)).toEqual(['a', 'b']);
      expect(svc.cardsFor('tu2').map((c) => c.cardId)).toEqual(['c']);
    });

    it('returns the same empty array for a miss or a missing id', () => {
      svc.seedFromHydration([card({ toolUseId: 'tu1' })]);
      // Stable reference — a fresh array each call would churn the frame's
      // computed on every change-detection pass.
      expect(svc.cardsFor('nope')).toBe(svc.cardsFor('other'));
      expect(svc.cardsFor(undefined)).toEqual([]);
    });
  });
});
