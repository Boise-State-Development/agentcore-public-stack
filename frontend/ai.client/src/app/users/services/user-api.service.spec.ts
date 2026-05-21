import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { UserApiService } from './user-api.service';
import { ConfigService } from '../../services/config.service';

describe('UserApiService', () => {
  let service: UserApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        UserApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(UserApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should search users', () => {
    const mockResponse = { users: [], total: 0 };
    
    service.searchUsers('test').subscribe(result => {
      expect(result).toEqual(mockResponse);
    });

    const req = httpMock.expectOne('http://localhost:8000/users/search?q=test&limit=20');
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should search users with custom limit', () => {
    const mockResponse = { users: [], total: 0 };
    
    service.searchUsers('test', 10).subscribe(result => {
      expect(result).toEqual(mockResponse);
    });

    const req = httpMock.expectOne('http://localhost:8000/users/search?q=test&limit=10');
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });
});