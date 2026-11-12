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
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';

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

  it('names a provider by its catalog display name, title-casing the id until then', async () => {
    expect(service.displayNameFor('google-tasks')).toBe('Google Tasks');

    isAuthenticated.set(true);
    service.ensureLoaded();
    TestBed.tick();
    await vi.waitFor(() =>
      httpMock.expectOne(LIST_URL).flush({
        connectors: [{ provider_id: 'google-tasks', display_name: 'Google Tasks For Faculty' }],
      }),
    );

    await vi.waitFor(() => expect(service.displayNameFor('google-tasks')).toBe('Google Tasks For Faculty'));
    expect(service.displayNameFor('zoom_hosted')).toBe('Zoom Hosted');
  });

  it('falls back to the title-cased id when the list fails', async () => {
    isAuthenticated.set(true);
    service.ensureLoaded();
    TestBed.tick();
    await vi.waitFor(() =>
      httpMock.expectOne(LIST_URL).flush('boom', { status: 500, statusText: 'Server Error' }),
    );

    await vi.waitFor(() => expect(service.connectorsResource.status()).toBe('error'));
    expect(service.displayNameFor('google-tasks')).toBe('Google Tasks');
  });

  it('probes connector status without the global error toast', async () => {
    // A 403 on a status probe (tool granted, connector's roles exclude the
    // caller) once raised an "Access Denied" toast per provider on every
    // load of the Tools page. Callers already treat a failure as inert.
    const pending = service.getStatus('google-tasks');
    const req = httpMock.expectOne(`${API}/connectors/google-tasks/status`);
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush(
      { detail: 'You do not have access to this connector' },
      { status: 403, statusText: 'Forbidden' },
    );
    await expect(pending).rejects.toBeTruthy();
  });
});
