import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { AgentMentionService } from './agent-mention.service';
import { AgentService } from './agent.service';
import { AgentPinService } from './agent-pin.service';
import { Agent } from '../models/agent.model';
import { PinnedAgent } from '../models/store.model';

function agent(partial: Partial<Agent> & { agentId: string; name: string }): Agent {
  return {
    ownerName: 'Ada Author',
    description: '',
    bindings: [],
    visibility: 'PRIVATE',
    tags: [],
    starters: [],
    usageCount: 0,
    status: 'COMPLETE',
    createdAt: '2026-07-01T00:00:00Z',
    updatedAt: '2026-07-01T00:00:00Z',
    ...partial,
  } as Agent;
}

function pin(partial: Partial<PinnedAgent> & { agentId: string; name: string }): PinnedAgent {
  return {
    category: 'Teaching',
    source: 'user',
    locked: false,
    ...partial,
  } as PinnedAgent;
}

/**
 * The `@` menu's candidate list (Marketplace D11).
 *
 * Scope is the whole design here: your own Agents plus everything pinned, and *not* the
 * store. The cases below are the ways that scope quietly widens or narrows — a draft
 * becoming mentionable, an Agent appearing twice under two groups, or a role-seeded pin
 * being filtered out, which would defeat the point of seeding a role in the first place.
 */
describe('AgentMentionService', () => {
  let service: AgentMentionService;
  let agents: ReturnType<typeof signal<Agent[]>>;
  let pins: ReturnType<typeof signal<PinnedAgent[]>>;
  let loadAgents: ReturnType<typeof vi.fn>;
  let loadPins: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    agents = signal<Agent[]>([]);
    pins = signal<PinnedAgent[]>([]);
    loadAgents = vi.fn().mockResolvedValue(undefined);
    loadPins = vi.fn().mockResolvedValue([]);

    TestBed.configureTestingModule({
      providers: [
        AgentMentionService,
        { provide: AgentService, useValue: { agents$: agents.asReadonly(), loadAgents } },
        { provide: AgentPinService, useValue: { pins: pins.asReadonly(), load: loadPins } },
      ],
    });
    service = TestBed.inject(AgentMentionService);
  });

  it('offers your own agents and your pins, own first', () => {
    agents.set([agent({ agentId: 'ast-own', name: 'My Grader' })]);
    pins.set([pin({ agentId: 'ast-pinned', name: 'Policy Lookup' })]);

    expect(service.mentionable().map((entry) => [entry.agentId, entry.group])).toEqual([
      ['ast-own', 'own'],
      ['ast-pinned', 'pinned'],
    ]);
  });

  it('lists an agent you own and also pinned exactly once', () => {
    agents.set([agent({ agentId: 'ast-1', name: 'My Grader' })]);
    pins.set([pin({ agentId: 'ast-1', name: 'My Grader' })]);

    const rows = service.mentionable();
    expect(rows).toHaveLength(1);
    // The own row wins: it is the group that explains why you can edit it.
    expect(rows[0].group).toBe('own');
  });

  it('includes role-seeded pins', () => {
    // The whole payoff of seeding a role (D9): a member who has never opened the store
    // still finds their department's agent by typing `@`.
    pins.set([pin({ agentId: 'ast-seeded', name: 'Registrar Helper', source: 'role' })]);

    expect(service.mentionable().map((entry) => entry.agentId)).toEqual(['ast-seeded']);
  });

  it('excludes drafts', () => {
    agents.set([
      agent({ agentId: 'ast-draft', name: 'Half Built', status: 'DRAFT' }),
      agent({ agentId: 'ast-done', name: 'Finished' }),
    ]);

    expect(service.mentionable().map((entry) => entry.agentId)).toEqual(['ast-done']);
  });

  it('shows the publisher for a pin and the tagline for your own', () => {
    agents.set([agent({ agentId: 'ast-own', name: 'Mine', tagline: 'Grades essays' })]);
    pins.set([
      pin({
        agentId: 'ast-pin',
        name: 'Theirs',
        tagline: 'Finds policy',
        publisher: { label: 'Registrar', kind: 'department', verified: true },
      }),
    ]);

    expect(service.mentionable().map((entry) => entry.subtitle)).toEqual([
      'Grades essays',
      'Registrar',
    ]);
  });

  describe('search', () => {
    beforeEach(() => {
      agents.set([
        agent({ agentId: 'ast-1', name: 'Policy Lookup', tagline: 'cites university policy' }),
        agent({ agentId: 'ast-2', name: 'Grading Assistant', tagline: 'policy-free grading' }),
      ]);
    });

    it('ranks a name prefix above a name substring above a subtitle match', () => {
      agents.update((current) => [
        ...current,
        agent({ agentId: 'ast-3', name: 'Campus Policy Bot' }),
      ]);

      expect(service.search('policy').map((entry) => entry.agentId)).toEqual([
        'ast-1', // name prefix
        'ast-3', // name substring
        'ast-2', // subtitle only
      ]);
    });

    it('is case-insensitive', () => {
      expect(service.search('POLICY')[0].agentId).toBe('ast-1');
    });

    it('returns everything for an empty query — typing `@` alone opens the whole menu', () => {
      expect(service.search('')).toHaveLength(2);
    });

    it('returns nothing when nothing matches', () => {
      expect(service.search('zzz')).toEqual([]);
    });
  });

  it('warms both sources once, and survives one of them failing', async () => {
    loadAgents.mockRejectedValueOnce(new Error('down'));

    await service.load();
    await service.load();

    expect(loadAgents).toHaveBeenCalledTimes(1);
    expect(loadPins).toHaveBeenCalledTimes(1);
    // A composer affordance must not break because a list behind it did not load.
    expect(service.mentionable()).toEqual([]);
  });
});
