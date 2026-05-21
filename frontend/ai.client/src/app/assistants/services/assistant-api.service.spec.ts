import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AssistantApiService } from './assistant-api.service';
import { ConfigService } from '../../services/config.service';

describe('AssistantApiService', () => {
  let service: AssistantApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AssistantApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AssistantApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should create draft', () => {
    const mockAssistant = { id: '1', name: 'Test' };
    
    service.createDraft({}).subscribe(result => {
      expect(result).toEqual(mockAssistant);
    });

    const req = httpMock.expectOne('http://localhost:8000/assistants/draft');
    expect(req.request.method).toBe('POST');
    req.flush(mockAssistant);
  });

  it('should get assistants', () => {
    const mockResponse = { assistants: [], nextToken: null };
    
    service.getAssistants().subscribe(result => {
      expect(result).toEqual(mockResponse);
    });

    const req = httpMock.expectOne('http://localhost:8000/assistants');
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should get assistant by id', () => {
    const mockAssistant = { id: '1', name: 'Test' };
    
    service.getAssistant('1').subscribe(result => {
      expect(result).toEqual(mockAssistant);
    });

    const req = httpMock.expectOne('http://localhost:8000/assistants/1');
    expect(req.request.method).toBe('GET');
    req.flush(mockAssistant);
  });

  it('should delete assistant', () => {
    service.deleteAssistant('1').subscribe();

    const req = httpMock.expectOne('http://localhost:8000/assistants/1');
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });
});