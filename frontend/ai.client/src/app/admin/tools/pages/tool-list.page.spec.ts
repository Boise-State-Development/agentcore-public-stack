import { describe, it, expect } from 'vitest';
import {
  gatewayBadgeFor,
  gatewayFailureReasonsFor,
  isTransientGatewayStatus,
  describeCount,
  capabilityRefreshSummary,
  type GatewayHealth,
} from './tool-list.page';
import { GatewayTargetStatus } from '../models/admin-tool.model';
import { ToolCapabilities } from '../../../services/tool-capability/tool-capability.service';

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
    expect(badge?.cls).toContain('state-success');
  });

  it('maps a still-syncing target to Syncing', () => {
    const badge = gatewayBadgeFor(status({ status: 'CREATING', healthy: false }));
    expect(badge?.label).toBe('Syncing');
    expect(badge?.failed).toBe(false);
    expect(badge?.cls).toContain('state-info');
  });

  it('maps a FAILED target to a red Failed badge carrying the reason in the title', () => {
    const reason = 'Authorization error when sending message';
    const badge = gatewayBadgeFor(
      status({ status: 'FAILED', healthy: false, statusReasons: [reason] }),
    );
    expect(badge?.label).toBe('Failed');
    expect(badge?.failed).toBe(true);
    expect(badge?.cls).toContain('state-danger');
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


/**
 * Capability refresh summary. Nothing else writes a tool's capability snapshot
 * and nothing re-runs on a schedule, so this button is the only way a server
 * that gained a prompt since its last probe stops showing an empty Prompts tab.
 * The summary is what tells the admin it actually worked.
 */
describe('describeCount', () => {
  it('pluralises, and says "no" rather than 0', () => {
    expect(describeCount(0, 'prompt')).toBe('no prompts');
    expect(describeCount(1, 'prompt')).toBe('1 prompt');
    expect(describeCount(8, 'prompt')).toBe('8 prompts');
  });
});

describe('capabilityRefreshSummary', () => {
  const snapshot = (over: Partial<ToolCapabilities>): ToolCapabilities => ({
    toolId: 'canvas_faculty',
    prompts: [],
    resources: [],
    supportsPrompts: true,
    supportsResources: true,
    truncated: false,
    ...over,
  });

  const prompt = (name: string) => ({ name, arguments: [] });

  it('reports what the probe found', () => {
    const summary = capabilityRefreshSummary(
      snapshot({ prompts: [prompt('a'), prompt('b')] }),
      'Canvas Faculty'
    );
    expect(summary).toBe('2 prompts, no resources');
  });

  it('says so when a server genuinely offers nothing', () => {
    expect(capabilityRefreshSummary(snapshot({}), 'Canvas Faculty')).toBe(
      'no prompts, no resources'
    );
  });

  it('flags a truncated listing rather than implying the count is complete', () => {
    const summary = capabilityRefreshSummary(
      snapshot({ prompts: [prompt('a')], truncated: true }),
      'Canvas Faculty'
    );
    expect(summary).toContain('(truncated)');
  });

  // The distinction that matters: an unreachable server leaves the previous
  // snapshot in place, so reporting "no prompts" would claim it offers none
  // when we never got an answer at all.
  it('reports an unreachable server as a failure, not as an empty result', () => {
    const summary = capabilityRefreshSummary(
      snapshot({ error: 'auth rejected', supportsPrompts: false }),
      'Canvas Faculty'
    );
    expect(summary).toBe('Could not reach Canvas Faculty: auth rejected');
    expect(summary).not.toContain('no prompts');
  });
});
