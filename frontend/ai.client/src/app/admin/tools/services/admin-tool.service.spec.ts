import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AdminToolService } from './admin-tool.service';
import { ConfigService } from '../../../services/config.service';
describe('AdminToolService', () => {
  let service: AdminToolService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AdminToolService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AdminToolService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should fetch tools', async () => {
    const mockResponse = { tools: [], total: 0 };
    const promise = service.fetchTools();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/tools/').flush(mockResponse);
    });
    expect(await promise).toEqual(mockResponse);
  });

  it('should fetch tool by id', async () => {
    const mockTool = { toolId: '1', name: 'Test Tool' };
    const promise = service.fetchTool('1');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/tools/1').flush(mockTool);
    });
    expect(await promise).toEqual(mockTool);
  });

  it('should create tool', async () => {
    const toolData = { name: 'New Tool' } as any;
    const mockTool = { toolId: '1', name: 'New Tool' };
    const promise = service.createTool(toolData);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/tools/').flush(mockTool);
    });
    expect(await promise).toEqual(mockTool);
  });

  it('should update tool', async () => {
    const updates = { name: 'Updated Tool' } as any;
    const mockTool = { toolId: '1', name: 'Updated Tool' };
    const promise = service.updateTool('1', updates);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/tools/1').flush(mockTool);
    });
    expect(await promise).toEqual(mockTool);
  });

  it('should delete tool', async () => {
    const promise = service.deleteTool('1');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/tools/1?hard=false').flush(null);
    });
    await promise;
  });
});