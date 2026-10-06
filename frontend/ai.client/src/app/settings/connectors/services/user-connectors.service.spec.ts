import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { UserConnectorsService } from './user-connectors.service';
import { ConfigService } from '../../../services/config.service';
import { SessionService } from '../../../auth/session.service';

const API = 'http://localhost:8000';
const LIST_URL = `${API}/connectors/`;

describe('UserConnectorsService', () => {
  let service: UserConnectorsService;
  let httpMock: HttpTestingController;
  let isAuthenticated: ReturnType<typeof signal<boolean>>;

  beforeEach(() => {
    isAuthenticated = signal(false);
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        UserConnectorsService,
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
        { provide: SessionService, useValue: { isAuthenticated } },
      ],
    });
    service = TestBed.inject(UserConnectorsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({ connectors: [] });
    });
    TestBed.resetTestingModule();
  });

  it('does not list connectors while there is no session', async () => {
    service.ensureLoaded();
    TestBed.tick();
    await Promise.resolve();
    await Promise.resolve();

    httpMock.expectNone(LIST_URL);
    expect(service.connectorsResource.status()).toBe('idle');
  });

  it('does not list connectors until a surface asks for them', async () => {
    isAuthenticated.set(true);
    TestBed.tick();
    await Promise.resolve();
    await Promise.resolve();

    httpMock.expectNone(LIST_URL);
    expect(service.connectorsResource.status()).toBe('idle');
  });

  it('lists connectors once asked and the session bootstrap has a user', async () => {
    service.ensureLoaded();
    TestBed.tick();
    httpMock.expectNone(LIST_URL);

    isAuthenticated.set(true);
    TestBed.tick();
    await vi.waitFor(() =>
      httpMock.expectOne(LIST_URL).flush({
        connectors: [{ provider_id: 'google', display_name: 'Google' }],
      }),
    );
    await vi.waitFor(() =>
      expect(service.connectorsResource.value()?.[0]?.providerId).toBe('google'),
    );
  });
});
