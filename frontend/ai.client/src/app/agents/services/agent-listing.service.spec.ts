import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AgentListingService } from './agent-listing.service';
import { AgentApiService } from './agent-api.service';
import { AgentService } from './agent.service';
import { AgentStoreService } from './agent-store.service';
import { AgentCategory, AgentListingBlock } from '../models/store.model';

function category(id: string): AgentCategory {
  return { id, label: id, order: 0, enabled: true };
}

function listing(overrides: Partial<AgentListingBlock> = {}): AgentListingBlock {
  return {
    state: 'in_review',
    category: 'Teaching',
    publisherId: 'user-user-001',
    ...overrides,
  };
}

describe('AgentListingService', () => {
  let service: AgentListingService;
  let mockApi: {
    getListingPreflight: ReturnType<typeof vi.fn>;
    submitListing: ReturnType<typeof vi.fn>;
    withdrawListing: ReturnType<typeof vi.fn>;
  };
  let mockAgents: { patchAgent: ReturnType<typeof vi.fn> };
  let mockStore: { loadStoreFront: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockApi = {
      getListingPreflight: vi.fn(),
      submitListing: vi.fn(),
      withdrawListing: vi.fn(),
    };
    mockAgents = { patchAgent: vi.fn() };
    mockStore = { loadStoreFront: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        AgentListingService,
        { provide: AgentApiService, useValue: mockApi },
        { provide: AgentService, useValue: mockAgents },
        { provide: AgentStoreService, useValue: mockStore },
      ],
    });
    service = TestBed.inject(AgentListingService);
  });

  describe('the kill-switch probe', () => {
    it('starts unknown so no publication control renders before the answer', () => {
      expect(service.available()).toBeNull();
    });

    it('marks the marketplace available and keeps the categories', async () => {
      mockStore.loadStoreFront.mockResolvedValue({
        featured: [],
        categories: [category('Teaching'), category('Research')],
      });

      await service.loadCategories();

      expect(service.available()).toBe(true);
      expect(service.categories().map((c) => c.id)).toEqual(['Teaching', 'Research']);
    });

    it('marks the marketplace unavailable on a 404 without surfacing an error', async () => {
      // AGENT_MARKETPLACE_ENABLED=false 404s the routes as a set; the surface should
      // behave as if the feature does not exist rather than show a broken control.
      mockStore.loadStoreFront.mockRejectedValue({ status: 404 });

      await service.loadCategories();

      expect(service.available()).toBe(false);
      expect(service.categories()).toEqual([]);
    });

    it('hides the controls on a real outage too — submitting would fail as well', async () => {
      mockStore.loadStoreFront.mockRejectedValue({ status: 500 });

      await service.loadCategories();

      expect(service.available()).toBe(false);
    });

    it('does not re-probe after a 404 — the kill switch does not flip mid-session', async () => {
      mockStore.loadStoreFront.mockRejectedValue({ status: 404 });

      await service.loadCategories();
      await service.loadCategories();

      expect(mockStore.loadStoreFront).toHaveBeenCalledTimes(1);
    });

    it('loads once per session unless forced', async () => {
      mockStore.loadStoreFront.mockResolvedValue({ featured: [], categories: [category('Teaching')] });

      await service.loadCategories();
      await service.loadCategories();
      expect(mockStore.loadStoreFront).toHaveBeenCalledTimes(1);

      await service.loadCategories(true);
      expect(mockStore.loadStoreFront).toHaveBeenCalledTimes(2);
    });

    it('retries after a transient failure, since that was not a durable answer', async () => {
      mockStore.loadStoreFront.mockRejectedValueOnce({ status: 500 });
      await service.loadCategories();

      mockStore.loadStoreFront.mockResolvedValue({ featured: [], categories: [category('IT')] });
      await service.loadCategories();

      expect(service.available()).toBe(true);
    });
  });

  describe('submit', () => {
    it('reflects the new listing on the author card without a reload', async () => {
      const submitted = listing();
      mockApi.submitListing.mockReturnValue(
        of({ agentId: 'ast-001', listing: submitted, exposedSkills: [] }),
      );

      const response = await service.submit('ast-001', { category: 'Teaching' });

      expect(mockApi.submitListing).toHaveBeenCalledWith('ast-001', { category: 'Teaching' });
      expect(mockAgents.patchAgent).toHaveBeenCalledWith('ast-001', { listing: submitted });
      expect(response.listing.state).toBe('in_review');
    });

    it('leaves the card alone when the submission is refused', async () => {
      // A memory_space binding added since the preflight, for instance (D7.2).
      mockApi.submitListing.mockReturnValue(throwError(() => ({ status: 400 })));

      await expect(service.submit('ast-001', { category: 'Teaching' })).rejects.toBeTruthy();
      expect(mockAgents.patchAgent).not.toHaveBeenCalled();
    });
  });

  describe('withdraw', () => {
    it('patches the card back to private', async () => {
      const withdrawn = listing({ state: 'private' });
      mockApi.withdrawListing.mockReturnValue(of(withdrawn));

      const result = await service.withdraw('ast-001');

      expect(result.state).toBe('private');
      expect(mockAgents.patchAgent).toHaveBeenCalledWith('ast-001', { listing: withdrawn });
    });
  });

  describe('preflight', () => {
    it('returns the D7 answers verbatim — the dialog never re-derives them', async () => {
      mockApi.getListingPreflight.mockReturnValue(
        of({
          agentId: 'ast-001',
          exposedSkills: [{ ref: 'skill-a', label: 'Policy Citation Format' }],
          blockReason: null,
        }),
      );

      const preflight = await service.preflight('ast-001');

      expect(preflight.exposedSkills).toHaveLength(1);
      expect(preflight.blockReason).toBeNull();
    });
  });
});
