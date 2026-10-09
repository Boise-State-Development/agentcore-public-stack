import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { UserGrantHttpService } from './user-grant-http.service';
import { ConfigService } from '../../../services/config.service';
import { UserGrant } from '../models';

const GRANT: UserGrant = {
  userId: 'user-1',
  grantedTools: ['browse_web'],
  grantedModels: [],
  grantedSkills: [],
  expiresAt: null,
  note: '',
  grantedBy: 'admin@example.com',
  createdAt: '2026-10-09T00:00:00+00:00',
  updatedAt: '2026-10-09T00:00:00+00:00',
  active: true,
};

describe('UserGrantHttpService', () => {
  let service: UserGrantHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        UserGrantHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(UserGrantHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    TestBed.resetTestingModule();
  });

  it('reads one user’s grant', () => {
    let got: UserGrant | undefined;
    service.getGrant('user-1').subscribe((g) => (got = g));

    const req = httpMock.expectOne('http://localhost:8000/admin/user-grants/user-1');
    expect(req.request.method).toBe('GET');
    req.flush(GRANT);
    expect(got).toEqual(GRANT);
  });

  it('replaces the grant with a PUT carrying every list', () => {
    service
      .setGrant('user-1', {
        grantedTools: ['browse_web'],
        grantedModels: ['m'],
        grantedSkills: [],
        expiresAt: '2026-12-01T00:00:00.000Z',
        note: 'pilot',
      })
      .subscribe();

    const req = httpMock.expectOne('http://localhost:8000/admin/user-grants/user-1');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({
      grantedTools: ['browse_web'],
      grantedModels: ['m'],
      grantedSkills: [],
      expiresAt: '2026-12-01T00:00:00.000Z',
      note: 'pilot',
    });
    req.flush(GRANT);
  });

  it('deletes the grant', () => {
    service.deleteGrant('user-1').subscribe();

    const req = httpMock.expectOne('http://localhost:8000/admin/user-grants/user-1');
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  it('encodes the user id in the path', () => {
    service.getGrant('a b/c').subscribe();
    httpMock.expectOne('http://localhost:8000/admin/user-grants/a%20b%2Fc').flush(GRANT);
  });
});
