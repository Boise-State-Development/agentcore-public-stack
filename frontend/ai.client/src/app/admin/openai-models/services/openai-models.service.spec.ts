import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { OpenAIModelsService } from './openai-models.service';
import { ConfigService } from '../../../services/config.service';
describe('OpenAIModelsService', () => {
  let service: OpenAIModelsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        OpenAIModelsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(OpenAIModelsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should get openai models', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.getOpenAIModels();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/openai/models').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get openai models with params', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.getOpenAIModels({ maxResults: 20 });
    await vi.waitFor(() => {
      const req = httpMock.expectOne(req => req.url === 'http://localhost:8000/admin/openai/models');
      expect(req.request.params.get('max_results')).toBe('20');
      req.flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should update models params', () => {
    service.updateModelsParams({ maxResults: 50 });
    expect(service['modelsParams']().maxResults).toBe(50);
  });

  it('should reset models params', () => {
    service.updateModelsParams({ maxResults: 50 });
    service.resetModelsParams();
    expect(service['modelsParams']()).toEqual({});
  });
});