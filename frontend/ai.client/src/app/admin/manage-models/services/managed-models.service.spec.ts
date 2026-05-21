import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ManagedModelsService } from './managed-models.service';
import { ConfigService } from '../../../services/config.service';
describe('ManagedModelsService', () => {
  let service: ManagedModelsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ManagedModelsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ManagedModelsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({});
    }); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should fetch managed models', async () => {
    const mockResponse = { models: [], totalCount: 0 };
    
    const promise = service.fetchManagedModels();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/managed-models').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should create model', async () => {
    const mockModel = { modelId: 'test', displayName: 'Test' } as any;
    const mockResponse = { ...mockModel, id: '1' };
    
    const promise = service.createModel(mockModel);
    httpMock.expectOne('http://localhost:8000/admin/managed-models').flush(mockResponse);
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get model by id', async () => {
    const mockResponse = { modelId: 'test', id: '1' };
    
    const promise = service.getModel('1');
    httpMock.expectOne('http://localhost:8000/admin/managed-models/1').flush(mockResponse);
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should update model', async () => {
    const mockResponse = { modelId: 'test', id: '1' };
    
    const promise = service.updateModel('1', { displayName: 'Updated' } as any);
    httpMock.expectOne('http://localhost:8000/admin/managed-models/1').flush(mockResponse);
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should delete model', async () => {
    const promise = service.deleteModel('1');
    httpMock.expectOne('http://localhost:8000/admin/managed-models/1').flush(null);
    
    await promise;
  });
});