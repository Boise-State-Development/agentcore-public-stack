import { describe, it, expect } from 'vitest';
import {
  gatewayBadgeFor,
  gatewayFailureReasonsFor,
  isTransientGatewayStatus,
  type GatewayHealth,
} from './tool-list.page';
import { GatewayTargetStatus } from '../models/admin-tool.model';

/**
 * Gateway target health badge mapping (Tier-1 UX for issue #419): turn the live
 * AgentCore Gateway target status into a row badge so a FAILED target is visible
 * to admins instead of only surfacing later as "the agent can't see the tool".
 */
describe('gatewayBadgeFor', () => {
  const status = (over: Partial<GatewayTargetStatus>): GatewayTargetStatus => ({
    targetId: 't',
    status: 'READY',
    statusReasons: [],
    healthy: true,
    ...over,
  });

  it('returns null before health is known', () => {
    expect(gatewayBadgeFor(undefined)).toBeNull();
  });

  it('maps the transient UI states', () => {
    expect(gatewayBadgeFor('loading')?.label).toBe('Checking…');
    expect(gatewayBadgeFor('loading')?.failed).toBe(false);
    expect(gatewayBadgeFor('error')?.label).toBe('Unknown');
    expect(gatewayBadgeFor('error')?.failed).toBe(false);
  });

  it('maps a healthy target to Ready', () => {
    const badge = gatewayBadgeFor(status({ status: 'READY', healthy: true }));
    expect(badge?.label).toBe('Ready');
    expect(badge?.failed).toBe(false);
    expect(badge?.cls).toContain('green');
  });

  it('maps a still-syncing target to Syncing', () => {
    const badge = gatewayBadgeFor(status({ status: 'CREATING', healthy: false }));
    expect(badge?.label).toBe('Syncing');
    expect(badge?.failed).toBe(false);
    expect(badge?.cls).toContain('blue');
  });

  it('maps a FAILED target to a red Failed badge carrying the reason in the title', () => {
    const reason = 'Authorization error when sending message';
    const badge = gatewayBadgeFor(
      status({ status: 'FAILED', healthy: false, statusReasons: [reason] }),
    );
    expect(badge?.label).toBe('Failed');
    expect(badge?.failed).toBe(true);
    expect(badge?.cls).toContain('red');
    expect(badge?.title).toBe(reason);
  });

  it('maps a MISSING target distinctly', () => {
    const badge = gatewayBadgeFor(status({ status: 'MISSING', healthy: false }));
    expect(badge?.label).toBe('Missing');
    expect(badge?.failed).toBe(true);
  });
});

describe('gatewayFailureReasonsFor', () => {
  it('returns reasons only for an unhealthy target', () => {
    expect(gatewayFailureReasonsFor(undefined)).toBeNull();
    expect(gatewayFailureReasonsFor('loading')).toBeNull();
    expect(
      gatewayFailureReasonsFor({ targetId: 't', status: 'READY', statusReasons: [], healthy: true }),
    ).toBeNull();
    expect(
      gatewayFailureReasonsFor({
        targetId: 't',
        status: 'FAILED',
        statusReasons: ['a', 'b'],
        healthy: false,
      }),
    ).toBe('a b');
  });
});

describe('isTransientGatewayStatus', () => {
  it('flags settling statuses case-insensitively', () => {
    expect(isTransientGatewayStatus('CREATING')).toBe(true);
    expect(isTransientGatewayStatus('updating')).toBe(true);
    expect(isTransientGatewayStatus('SYNCHRONIZING')).toBe(true);
    expect(isTransientGatewayStatus('READY')).toBe(false);
    expect(isTransientGatewayStatus('FAILED')).toBe(false);
  });
});

// Type-only guard: GatewayHealth must accept both the response and UI states.
const _h: GatewayHealth[] = ['loading', 'error'];
void _h;
