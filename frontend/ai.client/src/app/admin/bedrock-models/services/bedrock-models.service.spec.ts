import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { BedrockModelsService } from './bedrock-models.service';
import { ConfigService } from '../../../services/config.service';
describe('BedrockModelsService', () => {
  let service: BedrockModelsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        BedrockModelsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(BedrockModelsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should get bedrock models', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.getBedrockModels();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/bedrock/models').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get bedrock models with params', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.getBedrockModels({ byProvider: 'Anthropic', maxResults: 10 });
    await vi.waitFor(() => {
      const req = httpMock.expectOne(req => req.url === 'http://localhost:8000/admin/bedrock/models');
      expect(req.request.params.get('by_provider')).toBe('Anthropic');
      expect(req.request.params.get('max_results')).toBe('10');
      req.flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should update models params', () => {
    service.updateModelsParams({ byProvider: 'Anthropic' });
    expect(service['modelsParams']().byProvider).toBe('Anthropic');
  });

  it('should reset models params', () => {
    service.updateModelsParams({ byProvider: 'Anthropic' });
    service.resetModelsParams();
    expect(service['modelsParams']()).toEqual({});
  });
});