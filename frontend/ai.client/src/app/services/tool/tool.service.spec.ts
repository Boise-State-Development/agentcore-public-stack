import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ToolService, Tool, ToolsResponse } from './tool.service';
import { ConfigService } from '../config.service';
import { signal } from '@angular/core';

describe('ToolService', () => {
  let service: ToolService;
  let httpMock: HttpTestingController;

  const mockTools: Tool[] = [
    { toolId: 'search-web', displayName: 'Web Search', description: 'Search', category: 'search', icon: null, protocol: 'local', status: 'active', grantedBy: ['user'], enabledByDefault: true, userEnabled: null, isEnabled: true },
    { toolId: 'code-interp', displayName: 'Code Interpreter', description: 'Code', category: 'code', icon: null, protocol: 'aws_sdk', status: 'active', grantedBy: ['admin'], enabledByDefault: false, userEnabled: true, isEnabled: true },
  ];

  const mockResponse: ToolsResponse = { tools: mockTools, categories: ['search', 'code'], appRolesApplied: ['user'] };

  async function setup() {
    await setupWith(mockResponse);
  }

  async function setupWith(response: ToolsResponse) {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ToolService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });

    service = TestBed.inject(ToolService);
    httpMock = TestBed.inject(HttpTestingController);

    // Flush microtasks so constructor's async loadTools() makes the HTTP call
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/tools/').flush(response);
    });
  }

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  describe('loadTools', () => {
    beforeEach(setup);

    it('should load tools from constructor', () => {
      expect(service.tools()).toEqual(mockTools);
      expect(service.initialized()).toBe(true);
      expect(service.loading()).toBe(false);
    });

    it('should handle error', async () => {
      const promise = service.loadTools();
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/').error(new ProgressEvent('error'));
      });
      await promise;
      expect(service.error()).toBeTruthy();
    });

    it('dedupes a concurrent load to one request, and joins it rather than returning early', async () => {
      // Was: "should not load if already loading" — a second caller returned
      // immediately, with `tools()` still empty. Concurrency is still deduped
      // to one request, but the second caller now waits for the answer.
      const first = service.loadTools();
      let listWasEmptyOnResolve: boolean | null = null;
      const second = service.loadTools().then(() => {
        listWasEmptyOnResolve = service.tools().length === 0;
      });

      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/').flush(mockResponse);
      });
      await Promise.all([first, second]);

      httpMock.expectNone('http://localhost:8000/tools/');
      expect(listWasEmptyOnResolve).toBe(false);
    });
  });

  describe('agent binding lock', () => {
    beforeEach(setup);

    it('is unlocked by default', () => {
      expect(service.agentLocked()).toBe(false);
    });

    it('locks enabledToolIds to the bound set (replace semantics)', () => {
      service.lockToAgentTools(['code-interp']);
      expect(service.agentLocked()).toBe(true);
      // Replaces the user's set entirely, even though search-web is user-enabled.
      expect(service.enabledToolIds()).toEqual(['code-interp']);
      expect(service.enabledCount()).toBe(1);
    });

    it('reflects the bound set in per-tool shown state', () => {
      service.lockToAgentTools(['code-interp']);
      const search = service.getTool('search-web')!;
      const code = service.getTool('code-interp')!;
      expect(service.isToolShownEnabled(search)).toBe(false);
      expect(service.isToolShownEnabled(code)).toBe(true);
    });

    it('ignores toggles while locked (no HTTP)', async () => {
      service.lockToAgentTools(['code-interp']);
      await service.toggleTool('search-web');
      httpMock.expectNone('http://localhost:8000/tools/preferences');
      expect(service.enabledToolIds()).toEqual(['code-interp']);
    });

    it('filters visibleTools to only the bound tools while locked', () => {
      // Unlocked: every accessible tool is visible.
      expect(service.visibleTools().map(t => t.toolId).sort()).toEqual(['code-interp', 'search-web']);
      service.lockToAgentTools(['code-interp']);
      // Locked: only the bound tool is shown (the rest are hidden, not greyed).
      expect(service.visibleTools().map(t => t.toolId)).toEqual(['code-interp']);
    });

    it('restores the user set when cleared', () => {
      service.lockToAgentTools(['code-interp']);
      service.clearAgentLock();
      expect(service.agentLocked()).toBe(false);
      // Back to the per-tool computation (both mock tools are enabled).
      expect(service.enabledToolIds().sort()).toEqual(['code-interp', 'search-web']);
      expect(service.visibleTools().length).toBe(2);
    });

    it('locks to an empty set (agent with no tools ≠ free-select)', () => {
      service.lockToAgentTools([]);
      expect(service.agentLocked()).toBe(true);
      expect(service.enabledToolIds()).toEqual([]);
    });
  });

  describe('a project that binds no tools', () => {
    // search-web is admin-pinned here; code-interp is only the member's own pick.
    beforeEach(() =>
      setupWith({
        ...mockResponse,
        tools: mockTools.map(t => (t.toolId === 'search-web' ? { ...t, alwaysOn: true } : t)),
      }),
    );

    it("keeps the admin's always-on tools and drops the member's picks", () => {
      service.lockToAgentTools([], { keepAlwaysOn: true });
      expect(service.agentLocked()).toBe(true);
      expect(service.enabledToolIds()).toEqual(['search-web']);
      expect(service.visibleTools().map(t => t.toolId)).toEqual(['search-web']);
      expect(service.isToolShownEnabled(service.getTool('code-interp')!)).toBe(false);
    });

    it('an ordinary empty lock still leaves the pinned tools out', () => {
      service.lockToAgentTools([]);
      expect(service.enabledToolIds()).toEqual([]);
    });

    it('a later lock without the option forgets it', () => {
      service.lockToAgentTools([], { keepAlwaysOn: true });
      service.clearAgentLock();
      service.lockToAgentTools(['code-interp']);
      expect(service.enabledToolIds()).toEqual(['code-interp']);
    });
  });

  describe('toggleTool', () => {
    beforeEach(setup);

    it('should optimistically update and save', async () => {
      const promise = service.toggleTool('search-web');
      expect(service.getTool('search-web')?.isEnabled).toBe(false);

      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/tools/preferences');
        expect(req.request.body).toEqual({ preferences: { 'search-web': false } });
        req.flush({});
      });
      await promise;
    });

    it('should revert on error', async () => {
      const promise = service.toggleTool('search-web');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/preferences').error(new ProgressEvent('error'));
      });
      await expect(promise).rejects.toThrow();
      expect(service.getTool('search-web')?.isEnabled).toBe(true);
    });
  });

  describe('computed signals', () => {
    beforeEach(setup);

    it('should compute enabledTools', () => {
      expect(service.enabledToolIds()).toEqual(['search-web', 'code-interp']);
      expect(service.enabledCount()).toBe(2);
    });

    it('should compute toolsByCategory', () => {
      const byCategory = service.toolsByCategory();
      expect(byCategory.get('search')?.length).toBe(1);
      expect(byCategory.get('code')?.length).toBe(1);
    });

    it('should compute categories sorted', () => {
      expect(service.categories()).toEqual(['code', 'search']);
    });
  });

  describe('getTool / isToolEnabled', () => {
    beforeEach(setup);

    it('should return tool by id', () => {
      expect(service.getTool('search-web')?.displayName).toBe('Web Search');
      expect(service.getTool('nonexistent')).toBeUndefined();
    });

    it('should check enabled state', () => {
      expect(service.isToolEnabled('search-web')).toBe(true);
      expect(service.isToolEnabled('nonexistent')).toBe(false);
    });
  });

  describe('per-tool enablement', () => {
    const baseServer: Tool = {
      toolId: 'gmail', displayName: 'Gmail', description: 'Email', category: 'utility',
      icon: null, protocol: 'mcp_external', status: 'active', grantedBy: ['user'],
      enabledByDefault: false, userEnabled: null, isEnabled: true,
    };

    async function setupServer(server: Tool) {
      TestBed.configureTestingModule({
        providers: [
          provideHttpClient(),
          provideHttpClientTesting(),
          ToolService,
          { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        ],
      });
      service = TestBed.inject(ToolService);
      httpMock = TestBed.inject(HttpTestingController);
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/').flush({
          tools: [server], categories: ['utility'], appRolesApplied: ['user'],
        });
      });
    }

    it('emits scoped ids for a partially-enabled server', async () => {
      await setupServer({
        ...baseServer,
        serverTools: [
          { name: 'send', enabled: true },
          { name: 'search', enabled: true },
          { name: 'draft', enabled: false },
        ],
      });
      expect(service.enabledToolIds()).toEqual(['gmail::send', 'gmail::search']);
    });

    it('emits the bare id when every tool is enabled', async () => {
      await setupServer({
        ...baseServer,
        serverTools: [
          { name: 'send', enabled: true },
          { name: 'search', enabled: true },
        ],
      });
      expect(service.enabledToolIds()).toEqual(['gmail']);
    });

    it('toggleServerTool saves a scoped preference and recomputes ids', async () => {
      await setupServer({
        ...baseServer,
        serverTools: [
          { name: 'send', enabled: true },
          { name: 'draft', enabled: false },
        ],
      });
      const promise = service.toggleServerTool('gmail', 'draft');
      expect(service.getTool('gmail')?.serverTools?.find(s => s.name === 'draft')?.enabled).toBe(true);
      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/tools/preferences');
        expect(req.request.body).toEqual({ preferences: { 'gmail::draft': true } });
        req.flush({});
      });
      await promise;
      // Both tools now enabled → collapses to the bare server id.
      expect(service.enabledToolIds()).toEqual(['gmail']);
    });

    it('whole-server toggle disables the server and every tool', async () => {
      await setupServer({
        ...baseServer,
        serverTools: [
          { name: 'send', enabled: true },
          { name: 'search', enabled: true },
        ],
      });
      const promise = service.toggleTool('gmail');
      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/tools/preferences');
        expect(req.request.body).toEqual({
          preferences: { 'gmail': false, 'gmail::send': false, 'gmail::search': false },
        });
        req.flush({});
      });
      await promise;
      expect(service.getTool('gmail')?.isEnabled).toBe(false);
      expect(service.enabledToolIds()).toEqual([]);
    });
  });

  // The first-turn race (#1160): a chat turn sent while the constructor's
  // `/tools/` fetch is still in flight used to be assembled from an empty list,
  // so turn 1 and turn 2 carried different `enabled_tools` — a rewritten
  // `toolConfig` prefix, paid at the cache-write premium.
  describe('load gating', () => {
    function configureOnly() {
      TestBed.configureTestingModule({
        providers: [
          provideHttpClient(),
          provideHttpClientTesting(),
          ToolService,
          { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        ],
      });
      service = TestBed.inject(ToolService);
      httpMock = TestBed.inject(HttpTestingController);
    }

    it('ensureLoaded joins the constructor load instead of resolving early', async () => {
      configureOnly();

      let listWasEmptyOnResolve: boolean | null = null;
      const gate = service.ensureLoaded().then(() => {
        listWasEmptyOnResolve = service.tools().length === 0;
      });

      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/').flush(mockResponse);
      });
      await gate;

      expect(listWasEmptyOnResolve).toBe(false);
      expect(service.enabledToolIds()).toEqual(['search-web', 'code-interp']);
    });

    it('does not issue a second request while the constructor load is in flight', async () => {
      configureOnly();

      const both = Promise.all([service.ensureLoaded(), service.loadTools()]);
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/').flush(mockResponse);
      });
      await both;

      httpMock.verify();
    });

    it('ensureLoaded is a no-op once loaded', async () => {
      await setup();

      await service.ensureLoaded();

      httpMock.verify();
      expect(service.enabledToolIds()).toEqual(['search-web', 'code-interp']);
    });

    it('ensureLoaded resolves rather than rejecting when the load fails', async () => {
      configureOnly();

      const gate = service.ensureLoaded();
      await vi.waitFor(() => {
        httpMock
          .expectOne('http://localhost:8000/tools/')
          .error(new ProgressEvent('error'));
      });

      await expect(gate).resolves.toBeUndefined();
      expect(service.enabledToolIds()).toEqual([]);
    });
  });

  describe('reload', () => {
    beforeEach(setup);

    it('should reset initialized and reload', async () => {
      const promise = service.reload();
      expect(service.initialized()).toBe(false);
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/tools/').flush(mockResponse);
      });
      await promise;
      expect(service.initialized()).toBe(true);
    });
  });
});
