import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { UserHttpService } from './user-http.service';
import { ConfigService } from '../../../services/config.service';

describe('UserHttpService', () => {
  let service: UserHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        UserHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(UserHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should list users', () => {
    const mockResponse = { users: [{ id: 'user1', email: 'test@example.com' }], total: 1 };

    service.listUsers({ limit: 10 }).subscribe(response => {
      expect(response).toEqual(mockResponse);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/users?limit=10');
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should search by email', () => {
    const mockResponse = { users: [{ id: 'user1', email: 'test@example.com' }], total: 1 };

    service.searchByEmail('test@example.com').subscribe(response => {
      expect(response).toEqual(mockResponse);
    });

    const req = httpMock.expectOne(r => r.url.includes('/admin/users/search') && r.params.get('email') === 'test@example.com');
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should get user detail', () => {
    const mockDetail = { user: { id: 'user1', email: 'test@example.com' }, costSummary: {} };

    service.getUserDetail('user1').subscribe(detail => {
      expect(detail).toEqual(mockDetail);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/users/user1');
    expect(req.request.method).toBe('GET');
    req.flush(mockDetail);
  });

  it('should list domains', () => {
    const mockDomains = ['example.com', 'test.org'];

    service.listDomains(50).subscribe(domains => {
      expect(domains).toEqual(mockDomains);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/users/domains/list?limit=50');
    expect(req.request.method).toBe('GET');
    req.flush(mockDomains);
  });
});