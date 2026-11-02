import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { SkillService, UserSkill, SkillsResponse } from './skill.service';
import { ConfigService } from '../config.service';
import { signal } from '@angular/core';

describe('SkillService', () => {
  let service: SkillService;
  let httpMock: HttpTestingController;

  const mockSkills: UserSkill[] = [
    { skillId: 'pdf_workflows', displayName: 'PDF Workflows', description: 'Work with PDFs', category: 'document', userEnabled: null, isEnabled: true },
    { skillId: 'web_research', displayName: 'Web Research', description: 'Research the web', category: 'research', userEnabled: false, isEnabled: false },
  ];

  const mockResponse: SkillsResponse = { skills: mockSkills, totalCount: 2 };

  function configure() {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        SkillService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });

    service = TestBed.inject(SkillService);
    httpMock = TestBed.inject(HttpTestingController);
  }

  async function setup() {
    configure();
    const promise = service.loadSkills();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/skills/').flush(mockResponse);
    });
    await promise;
  }

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  describe('construction', () => {
    it('does not fetch skills on construction (loading is deferred)', () => {
      configure();
      // No /skills/ request is issued on construction.
      httpMock.verify();
      expect(service.initialized()).toBe(false);
      expect(service.skills()).toEqual([]);
    });
  });

  describe('agent binding lock', () => {
    beforeEach(setup);

    it('is unlocked by default', () => {
      expect(service.agentLocked()).toBe(false);
    });

    it('locks enabledSkillIds to the bound set (replace semantics)', () => {
      // web_research is user-disabled; the lock forces exactly the bound set.
      service.lockToAgentSkillIds(['web_research']);
      expect(service.agentLocked()).toBe(true);
      expect(service.enabledSkillIds()).toEqual(['web_research']);
      expect(service.enabledCount()).toBe(1);
    });

    it('reflects the bound set in per-skill shown state', () => {
      service.lockToAgentSkillIds(['web_research']);
      const pdf = service.skills().find(s => s.skillId === 'pdf_workflows')!;
      const web = service.skills().find(s => s.skillId === 'web_research')!;
      expect(service.isSkillShownEnabled(pdf)).toBe(false);
      expect(service.isSkillShownEnabled(web)).toBe(true);
    });

    it('ignores toggles while locked (no HTTP)', async () => {
      service.lockToAgentSkillIds(['web_research']);
      await service.toggleSkill('pdf_workflows');
      httpMock.expectNone('http://localhost:8000/skills/preferences');
      expect(service.enabledSkillIds()).toEqual(['web_research']);
    });

    it('filters visibleSkills to only the bound skills while locked', () => {
      expect(service.visibleSkills().map(s => s.skillId).sort()).toEqual(['pdf_workflows', 'web_research']);
      service.lockToAgentSkillIds(['web_research']);
      expect(service.visibleSkills().map(s => s.skillId)).toEqual(['web_research']);
    });

    it('locks to no skills at all for a project that binds none', () => {
      // A project never runs on the member's own skills, so an empty lock is a real lock.
      service.lockToAgentSkillIds([]);
      expect(service.agentLocked()).toBe(true);
      expect(service.enabledSkillIds()).toEqual([]);
      expect(service.visibleSkills()).toEqual([]);
    });

    it('restores the user set when cleared', () => {
      service.lockToAgentSkillIds(['web_research']);
      service.clearAgentLock();
      expect(service.agentLocked()).toBe(false);
      // Back to per-skill state: only pdf_workflows is user-enabled.
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
      expect(service.visibleSkills().length).toBe(2);
    });
  });

  describe('loadSkills', () => {
    beforeEach(setup);

    it('should load skills', () => {
      expect(service.skills()).toEqual(mockSkills);
      expect(service.initialized()).toBe(true);
      expect(service.loading()).toBe(false);
    });

    it('should handle error', async () => {
      const promise = service.loadSkills();
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/').error(new ProgressEvent('error'));
      });
      await promise;
      expect(service.error()).toBeTruthy();
    });
  });

  // The first-turn race (#1160): a chat turn sent while `/skills/` is still in
  // flight used to be assembled from an empty list, disclosing no skills, while
  // the next turn disclosed the real ones — flipping the system prompt between
  // turn 1 and turn 2 and re-writing the cacheable prefix at the cache-write
  // premium.
  describe('load gating', () => {
    it('joins an in-flight load instead of resolving early', async () => {
      configure();

      const first = service.loadSkills();
      // The old guard was `if (this._loading()) return;` — this second call
      // resolved immediately, with `skills()` still empty.
      let listWasEmptyOnResolve: boolean | null = null;
      const second = service.loadSkills().then(() => {
        listWasEmptyOnResolve = service.skills().length === 0;
      });

      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/').flush(mockResponse);
      });
      await Promise.all([first, second]);

      expect(listWasEmptyOnResolve).toBe(false);
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
    });

    it('issues exactly one request for concurrent loads', async () => {
      configure();

      const both = Promise.all([service.loadSkills(), service.loadSkills()]);
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/').flush(mockResponse);
      });
      await both;

      httpMock.verify();
    });

    it('ensureLoaded starts the load when nothing has, and settles with the list', async () => {
      configure();

      const gate = service.ensureLoaded();
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/').flush(mockResponse);
      });
      await gate;

      expect(service.initialized()).toBe(true);
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
    });

    it('ensureLoaded does not resolve while the load is still in flight', async () => {
      configure();

      let settled = false;
      const gate = service.ensureLoaded().then(() => {
        settled = true;
      });
      // Drain the microtask queue: the gate must still be pending, because the
      // response has not been flushed.
      await Promise.resolve();
      await Promise.resolve();
      expect(settled).toBe(false);

      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/').flush(mockResponse);
      });
      await gate;
      expect(settled).toBe(true);
    });

    it('ensureLoaded is a no-op once loaded', async () => {
      await setup();

      await service.ensureLoaded();

      httpMock.verify();
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
    });

    it('ensureLoaded resolves rather than rejecting when the load fails', async () => {
      configure();

      const gate = service.ensureLoaded();
      await vi.waitFor(() => {
        httpMock
          .expectOne('http://localhost:8000/skills/')
          .error(new ProgressEvent('error'));
      });

      await expect(gate).resolves.toBeUndefined();
      expect(service.enabledSkillIds()).toEqual([]);
    });
  });

  describe('computed signals', () => {
    beforeEach(setup);

    it('should compute enabled skill ids from effective state', () => {
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
      expect(service.enabledCount()).toBe(1);
      expect(service.hasSkills()).toBe(true);
    });
  });

  describe('toggleSkill', () => {
    beforeEach(setup);

    it('should optimistically update and persist the preference', async () => {
      const promise = service.toggleSkill('pdf_workflows');
      expect(service.getSkill('pdf_workflows')?.isEnabled).toBe(false);

      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/skills/preferences');
        expect(req.request.method).toBe('PUT');
        expect(req.request.body).toEqual({ preferences: { pdf_workflows: false } });
        req.flush({});
      });
      await promise;
      expect(service.getSkill('pdf_workflows')?.userEnabled).toBe(false);
    });

    it('should revert on error', async () => {
      const promise = service.toggleSkill('pdf_workflows');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/preferences').error(new ProgressEvent('error'));
      });
      await expect(promise).rejects.toThrow();
      expect(service.getSkill('pdf_workflows')?.isEnabled).toBe(true);
      expect(service.getSkill('pdf_workflows')?.userEnabled).toBe(null);
    });
  });
  describe('resolved agent lock', () => {
    // The Agent resolved its bound skills for this viewer (`GET /agents/{id}/skills`).
    // `owners_style` is not in the viewer's own list at all: it is the owner's skill,
    // which is exactly the case the id lock could never show.
    const resolved = [
      { skillId: 'owners_style', displayName: "Owner's Style", description: 'House style', slug: 'owners-style' },
      { skillId: 'web_research', displayName: 'Web Research', description: 'Research the web', slug: 'web-research' },
    ];

    beforeEach(setup);

    it('renders the resolved entries, not the intersection with the user list', () => {
      service.lockToAgentSkills(resolved);
      expect(service.agentLocked()).toBe(true);
      expect(service.agentSkillsResolving()).toBe(false);
      expect(service.visibleSkills().map(s => s.skillId)).toEqual(['owners_style', 'web_research']);
      expect(service.enabledSkillIds()).toEqual(['owners_style', 'web_research']);
      expect(service.enabledCount()).toBe(2);
    });

    it('carries the server slug and shows every resolved row as on', () => {
      service.lockToAgentSkills(resolved);
      const rows = service.visibleSkills();
      expect(rows.map(s => s.slug)).toEqual(['owners-style', 'web-research']);
      expect(rows.every(s => service.isSkillShownEnabled(s))).toBe(true);
    });

    it('is resolving between the id lock and the resolved list', () => {
      service.lockToAgentSkillIds(['owners_style'], { resolving: true });
      expect(service.agentSkillsResolving()).toBe(true);
      // Until the list arrives the rows are what the user list can name: nothing here.
      expect(service.visibleSkills()).toEqual([]);
      service.lockToAgentSkills(resolved);
      expect(service.agentSkillsResolving()).toBe(false);
    });

    it('settles to the id lock when the resolution fails, keeping the lock', () => {
      service.lockToAgentSkillIds(['web_research'], { resolving: true });
      service.settleAgentLock();
      expect(service.agentLocked()).toBe(true);
      expect(service.agentSkillsResolving()).toBe(false);
      expect(service.visibleSkills().map(s => s.skillId)).toEqual(['web_research']);
    });

    it('ignores toggles while locked to resolved entries', async () => {
      service.lockToAgentSkills(resolved);
      await service.toggleSkill('web_research');
      httpMock.expectNone('http://localhost:8000/skills/preferences');
    });

    it('restores the user set when cleared', () => {
      service.lockToAgentSkills(resolved);
      service.clearAgentLock();
      expect(service.visibleSkills().map(s => s.skillId).sort()).toEqual(['pdf_workflows', 'web_research']);
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
    });
  });
});
