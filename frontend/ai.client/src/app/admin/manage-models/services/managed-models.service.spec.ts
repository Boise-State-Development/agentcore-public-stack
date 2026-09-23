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

  describe('reorderModels', () => {
    const ORDER_URL = 'http://localhost:8000/admin/managed-models/order';
    const model = (id: string) => ({ id, modelId: id, modelName: id }) as any;

    async function loadCatalog(ids: string[]): Promise<void> {
      await vi.waitFor(() => {
        httpMock
          .expectOne('http://localhost:8000/admin/managed-models')
          .flush({ models: ids.map(model), totalCount: ids.length });
      });
      await vi.waitFor(() => expect(service.getManagedModels().map(m => m.id)).toEqual(ids));
    }

    const listedIds = () => service.getManagedModels().map(m => m.id);

    it('reorders the list before the save returns', async () => {
      TestBed.tick();
      await loadCatalog(['a', 'b', 'c']);

      const done = service.reorderModels(['c', 'a', 'b']);

      expect(listedIds()).toEqual(['c', 'a', 'b']);
      const req = httpMock.expectOne(ORDER_URL);
      expect(req.request.method).toBe('PUT');
      expect(req.request.body).toEqual({ modelIds: ['c', 'a', 'b'] });
      req.flush(null);
      await done;
      expect(listedIds()).toEqual(['c', 'a', 'b']);
    });

    it('coalesces moves made during a save into one follow-up save of the latest order', async () => {
      TestBed.tick();
      await loadCatalog(['a', 'b', 'c']);

      const first = service.reorderModels(['b', 'a', 'c']);
      service.reorderModels(['b', 'c', 'a']);
      const last = service.reorderModels(['c', 'b', 'a']);

      // Only one request in flight, and nothing sent for the middle order.
      httpMock.expectOne(ORDER_URL).flush(null);
      await vi.waitFor(() => {
        const followUp = httpMock.expectOne(ORDER_URL);
        expect(followUp.request.body).toEqual({ modelIds: ['c', 'b', 'a'] });
        followUp.flush(null);
      });

      await Promise.all([first, last]);
      expect(listedIds()).toEqual(['c', 'b', 'a']);
      httpMock.expectNone(ORDER_URL);
    });

    it('reloads the catalog when the save fails', async () => {
      TestBed.tick();
      await loadCatalog(['a', 'b']);

      const done = service.reorderModels(['b', 'a']);
      httpMock.expectOne(ORDER_URL).flush('stale', { status: 409, statusText: 'Conflict' });

      await expect(done).rejects.toBeTruthy();
      // The server's order wins over the optimistic one.
      expect(listedIds()).toEqual(['b', 'a']);
      await loadCatalog(['a', 'b']);
    });
  });
});