import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { GeminiModelsService } from './gemini-models.service';
import { ConfigService } from '../../../services/config.service';
describe('GeminiModelsService', () => {
  let service: GeminiModelsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        GeminiModelsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(GeminiModelsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should get gemini models', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.getGeminiModels();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/gemini/models').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get gemini models with params', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.getGeminiModels({ maxResults: 15 });
    await vi.waitFor(() => {
      const req = httpMock.expectOne(req => req.url === 'http://localhost:8000/admin/gemini/models');
      expect(req.request.params.get('max_results')).toBe('15');
      req.flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should update models params', () => {
    service.updateModelsParams({ maxResults: 25 });
    expect(service['modelsParams']().maxResults).toBe(25);
  });

  it('should reset models params', () => {
    service.updateModelsParams({ maxResults: 25 });
    service.resetModelsParams();
    expect(service['modelsParams']()).toEqual({});
  });
});