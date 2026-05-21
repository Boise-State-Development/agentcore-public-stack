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
      httpMock.expectOne('http://localhost:8000/tools/').flush(mockResponse);
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

    it('should not load if already loading', async () => {
      service['_loading'].set(true);
      await service.loadTools();
      httpMock.expectNone('http://localhost:8000/tools/');
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
