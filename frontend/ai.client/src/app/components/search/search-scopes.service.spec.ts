// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ConfigService } from '../../services/config.service';
import { AgentPinService } from '../../agents/services/agent-pin.service';
import { PinnedAgent } from '../../agents/models/store.model';
import { SearchScopesService } from './search-scopes.service';

const pin = (agentId: string, name: string): PinnedAgent => ({
  agentId,
  name,
  tagline: `${name} tagline`,
  category: 'general',
  source: 'user',
  locked: false,
});

describe('SearchScopesService', () => {
  let http: HttpTestingController;
  let service: SearchScopesService;
  let pinLoad: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    pinLoad = vi.fn().mockResolvedValue([pin('own-1', 'Mine, pinned'), pin('store-1', 'Store agent')]);
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => '/api' } },
        { provide: AgentPinService, useValue: { load: pinLoad } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(SearchScopesService);
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  const flushMicrotasks = () => new Promise(resolve => setTimeout(resolve));

  it('agents: own and shared from GET /agents (drafts included) plus pins, fetched once', async () => {
    const done = service.loadAgents();
    expect(service.agentsState()).toBe('loading');
    const req = http.expectOne(r => r.url === '/api/agents');
    expect(req.request.params.get('include_drafts')).toBe('true');
    req.flush({
      agents: [
        { agentId: 'own-1', name: 'Mine', description: 'Grades essays', status: 'COMPLETE' },
        { agentId: 'draft-1', name: 'Half done', description: '', status: 'DRAFT' },
        { agentId: 'shared-1', name: 'Theirs', description: 'Shared one', status: 'COMPLETE', isSharedWithMe: true },
      ],
    });
    await done;

    expect(service.agentsState()).toBe('ready');
    expect(service.agents().map(a => [a.agentId, a.tag, a.draft])).toEqual([
      ['own-1', null, false],
      ['draft-1', 'Draft', true],
      ['shared-1', 'Shared', false],
      ['store-1', 'Public', false],
    ]);
    expect(service.agents()[3].description).toBe('Store agent tagline');

    await service.loadAgents();
    http.expectNone(r => r.url === '/api/agents');
    expect(pinLoad).toHaveBeenCalledTimes(1);
  });

  it('agents: a 404 (agents switched off) is an empty list, not an error', async () => {
    pinLoad.mockResolvedValue([]);
    const done = service.loadAgents();
    http.expectOne(r => r.url === '/api/agents').flush('off', { status: 404, statusText: 'Not Found' });
    await done;
    expect(service.agentsState()).toBe('ready');
    expect(service.agents()).toEqual([]);
  });

  it('a failure holds until retryFailed, so it is not refetched on every keystroke', async () => {
    const done = service.loadArtifacts();
    http.expectOne('/api/artifacts/library').flush('boom', { status: 500, statusText: 'err' });
    await done;
    expect(service.artifactsState()).toBe('error');

    await service.loadArtifacts();
    http.expectNone('/api/artifacts/library');

    service.retryFailed();
    const again = service.loadArtifacts();
    http.expectOne('/api/artifacts/library').flush({
      artifacts: [
        {
          artifact_id: 'f1',
          version: 2,
          title: 'Notes',
          content_type: 'text/markdown',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-02T00:00:00Z',
          session_id: 's1',
        },
      ],
    });
    await again;
    await flushMicrotasks();
    expect(service.artifactsState()).toBe('ready');
    expect(service.artifacts().map(a => a.title)).toEqual(['Notes']);
  });
});
