import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AgentService } from './agent.service';
import { AgentApiService } from './agent-api.service';
import { Agent, BindableItem } from '../models/agent.model';

function stubAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    agentId: 'ast-001',
    ownerName: 'Alice',
    name: 'My Agent',
    description: 'Helpful',
    instructions: 'You are helpful.',
    bindings: [],
    visibility: 'PRIVATE',
    tags: [],
    starters: [],
    usageCount: 0,
    status: 'COMPLETE',
    createdAt: '2026-07-08T00:00:00Z',
    updatedAt: '2026-07-08T00:00:00Z',
    ...overrides,
  };
}

describe('AgentService', () => {
  let service: AgentService;
  let mockApi: {
    getAgents: ReturnType<typeof vi.fn>;
    getAgent: ReturnType<typeof vi.fn>;
    createDraft: ReturnType<typeof vi.fn>;
    createAgent: ReturnType<typeof vi.fn>;
    updateAgent: ReturnType<typeof vi.fn>;
    deleteAgent: ReturnType<typeof vi.fn>;
    getBindable: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockApi = {
      getAgents: vi.fn(),
      getAgent: vi.fn(),
      createDraft: vi.fn(),
      createAgent: vi.fn(),
      updateAgent: vi.fn(),
      deleteAgent: vi.fn(),
      getBindable: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [AgentService, { provide: AgentApiService, useValue: mockApi }],
    });
    service = TestBed.inject(AgentService);
  });

  it('loads agents and marks the feature accessible', async () => {
    const agent = stubAgent();
    mockApi.getAgents.mockReturnValue(of({ agents: [agent] }));

    await service.loadAgents();

    expect(service.agents$()).toEqual([agent]);
    expect(service.accessible$()).toBe(true);
    expect(service.loading$()).toBe(false);
  });

  it('marks the feature inaccessible on a 404 (kill switch off) without an error', async () => {
    mockApi.getAgents.mockReturnValue(throwError(() => ({ status: 404 })));

    await service.loadAgents();

    expect(service.accessible$()).toBe(false);
    expect(service.agents$()).toEqual([]);
    expect(service.error$()).toBeNull();
  });

  it('surfaces a real error for non-gating failures and leaves accessible unresolved', async () => {
    mockApi.getAgents.mockReturnValue(throwError(() => new Error('network blip')));

    await expect(service.loadAgents()).rejects.toThrow('network blip');
    expect(service.error$()).toBe('network blip');
    expect(service.accessible$()).toBeNull();
  });

  it('prepends a created agent to local state', async () => {
    mockApi.getAgents.mockReturnValue(of({ agents: [] }));
    await service.loadAgents();

    const created = stubAgent({ agentId: 'ast-new', name: 'New' });
    mockApi.createAgent.mockReturnValue(of(created));

    const result = await service.createAgent({ name: 'New', description: 'd', instructions: 'i' });

    expect(result).toEqual(created);
    expect(service.agents$()[0]).toEqual(created);
  });

  it('replaces an updated agent in local state', async () => {
    const agent = stubAgent();
    mockApi.getAgents.mockReturnValue(of({ agents: [agent] }));
    await service.loadAgents();

    const updated = stubAgent({ name: 'Renamed' });
    mockApi.updateAgent.mockReturnValue(of(updated));
    await service.updateAgent('ast-001', { name: 'Renamed' });

    expect(service.agents$()[0].name).toBe('Renamed');
  });

  it('removes a deleted agent from local state', async () => {
    const agent = stubAgent();
    mockApi.getAgents.mockReturnValue(of({ agents: [agent] }));
    await service.loadAgents();

    mockApi.deleteAgent.mockReturnValue(of(undefined));
    await service.deleteAgent('ast-001');

    expect(service.agents$()).toEqual([]);
  });

  it('memoises the bindable palette per kind and returns [] on failure', async () => {
    const items: BindableItem[] = [
      { kind: 'model', ref: 'us.anthropic.claude', label: 'Claude', description: 'Bedrock', meta: {} },
    ];
    mockApi.getBindable.mockReturnValue(of({ kind: 'model', items }));

    const first = await service.loadBindable('model');
    const second = await service.loadBindable('model');

    expect(first).toEqual(items);
    expect(second).toEqual(items);
    expect(mockApi.getBindable).toHaveBeenCalledTimes(1); // cached

    mockApi.getBindable.mockReturnValue(throwError(() => new Error('boom')));
    const skills = await service.loadBindable('skill');
    expect(skills).toEqual([]); // degrades gracefully
  });
});
