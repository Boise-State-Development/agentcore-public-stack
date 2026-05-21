import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ToolsService } from './tools.service';
import { ConfigService } from '../../../services/config.service';
describe('ToolsService', () => {
  let service: ToolsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ToolsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ToolsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should fetch catalog', async () => {
    const mockResponse = { tools: [], total: 0 };
    const promise = service.fetchCatalog();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/tools/catalog').flush(mockResponse);
    });
    expect(await promise).toEqual(mockResponse);
  });

  it('should get tools by category', () => {
    const tools = [
      { toolId: '1', category: 'search', name: 'Tool 1' },
      { toolId: '2', category: 'search', name: 'Tool 2' },
      { toolId: '3', category: 'analysis', name: 'Tool 3' }
    ];
    vi.spyOn(service, 'getTools').mockReturnValue(tools as any);
    
    const result = service.getToolsByCategory();
    expect(result.get('search')).toHaveLength(2);
    expect(result.get('analysis')).toHaveLength(1);
  });
});