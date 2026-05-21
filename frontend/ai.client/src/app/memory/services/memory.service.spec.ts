import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { MemoryService } from './memory.service';
import { ConfigService } from '../../services/config.service';
describe('MemoryService', () => {
  let service: MemoryService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        MemoryService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(MemoryService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({});
    }); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should fetch memory status', async () => {
    const mockStatus = { totalMemories: 10, strategies: [] };

    const statusPromise = service.fetchMemoryStatus();
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/memory/status').flush(mockStatus);
    });

    const status = await statusPromise;
    expect(status).toEqual(mockStatus);
  });

  it('should fetch all memories', async () => {
    const mockMemories = { memories: [], total: 0 };

    const memoriesPromise = service.fetchAllMemories(20);
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/memory?topK=20').flush(mockMemories);
    });

    const memories = await memoriesPromise;
    expect(memories).toEqual(mockMemories);
  });

  it('should search memories', async () => {
    const mockResults = { memories: [], total: 0 };
    const searchRequest = { query: 'test', topK: 5 };

    const searchPromise = service.searchMemories(searchRequest);
    
    await vi.waitFor(() => {
      const req = httpMock.expectOne('http://localhost:8000/memory/search');
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual(searchRequest);
      req.flush(mockResults);
    });

    const results = await searchPromise;
    expect(results).toEqual(mockResults);
  });

  it('should delete memory', async () => {
    const mockResponse = { success: true };

    const deletePromise = service.deleteMemory('record123');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/memory/record123').flush(mockResponse);
    });

    const response = await deletePromise;
    expect(response).toEqual(mockResponse);
  });
});