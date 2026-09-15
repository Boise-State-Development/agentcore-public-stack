import { describe, it, expect } from 'vitest';
import { reconcileToolRefs } from './tool-ref-reconcile';
import { Tool, ToolStatus } from '../../services/tool/tool.service';

/**
 * Build a minimal catalog Tool with a given id and status. Only the fields the
 * reconciler reads (`toolId`, `status`) matter; the rest are filled with inert
 * defaults so the object satisfies the `Tool` interface.
 */
function makeTool(toolId: string, status: ToolStatus): Tool {
  return {
    toolId,
    displayName: toolId,
    description: '',
    category: 'utility',
    icon: null,
    protocol: 'local',
    status,
    grantedBy: [],
    enabledByDefault: false,
    userEnabled: null,
    isEnabled: false,
  };
}

describe('reconcileToolRefs', () => {
  it('applies an active ref (no flag)', () => {
    const catalog = [makeTool('t.active', 'active')];
    const result = reconcileToolRefs(['t.active'], catalog);
    expect(result).toEqual({
      apply: ['t.active'],
      flagged: [],
      dropped: [],
    });
  });

  it('applies AND flags a deprecated ref', () => {
    const catalog = [makeTool('t.deprecated', 'deprecated')];
    const result = reconcileToolRefs(['t.deprecated'], catalog);
    expect(result).toEqual({
      apply: ['t.deprecated'],
      flagged: [{ ref: 't.deprecated', status: 'deprecated' }],
      dropped: [],
    });
  });

  it('applies AND flags a disabled ref', () => {
    const catalog = [makeTool('t.disabled', 'disabled')];
    const result = reconcileToolRefs(['t.disabled'], catalog);
    expect(result).toEqual({
      apply: ['t.disabled'],
      flagged: [{ ref: 't.disabled', status: 'disabled' }],
      dropped: [],
    });
  });

  it('applies AND flags a coming_soon ref', () => {
    const catalog = [makeTool('t.soon', 'coming_soon')];
    const result = reconcileToolRefs(['t.soon'], catalog);
    expect(result).toEqual({
      apply: ['t.soon'],
      flagged: [{ ref: 't.soon', status: 'coming_soon' }],
      dropped: [],
    });
  });

  it('drops a ref not present in the catalog', () => {
    const catalog = [makeTool('t.active', 'active')];
    const result = reconcileToolRefs(['t.unknown'], catalog);
    expect(result).toEqual({
      apply: [],
      flagged: [],
      dropped: ['t.unknown'],
    });
  });

  it('handles a mixed list and preserves input order', () => {
    const catalog = [
      makeTool('t.active', 'active'),
      makeTool('t.deprecated', 'deprecated'),
      makeTool('t.disabled', 'disabled'),
      makeTool('t.soon', 'coming_soon'),
    ];
    const refs = [
      't.unknown1',
      't.active',
      't.deprecated',
      't.unknown2',
      't.disabled',
      't.soon',
    ];

    const result = reconcileToolRefs(refs, catalog);

    // apply = every ref present in the catalog, in input order.
    expect(result.apply).toEqual([
      't.active',
      't.deprecated',
      't.disabled',
      't.soon',
    ]);
    // flagged = the non-active present subset, in input order.
    expect(result.flagged).toEqual([
      { ref: 't.deprecated', status: 'deprecated' },
      { ref: 't.disabled', status: 'disabled' },
      { ref: 't.soon', status: 'coming_soon' },
    ]);
    // dropped = unknown refs, in input order.
    expect(result.dropped).toEqual(['t.unknown1', 't.unknown2']);
  });

  it('is pure — does not mutate its inputs', () => {
    const catalog = [makeTool('t.active', 'active')];
    const refs = ['t.active'];
    const refsCopy = [...refs];
    reconcileToolRefs(refs, catalog);
    expect(refs).toEqual(refsCopy);
    expect(catalog.length).toBe(1);
  });
});
