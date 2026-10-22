import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { UserSettingsService, UserSettings } from './user-settings.service';
import { ConfigService } from './config.service';

const API = 'http://localhost:8000';
const URL = `${API}/users/me/settings`;

describe('UserSettingsService', () => {
  let service: UserSettingsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        UserSettingsService,
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
      ],
    });
    service = TestBed.inject(UserSettingsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  /** Flush every outstanding GET for the settings URL, returning the count. */
  const flushSettings = (body: UserSettings): number => {
    const reqs = httpMock.match(r => r.url === URL && r.method === 'GET');
    reqs.forEach(r => r.flush(body));
    return reqs.length;
  };

  // ── First-load de-duplication ─────────────────────────────────────────
  // Regression cover for the duplicate `/users/me/settings` on first load:
  // `settingsResource` fetched eagerly and `ModelService` fetched again once
  // `/models` landed, so the SPA read the same row twice per page load.

  it('issues one GET when several callers read on first load', async () => {
    const a = service.getSettings();
    const b = service.getSettings();
    const c = service.getSettings();

    expect(flushSettings({ defaultModelId: 'm-1' })).toBe(1);

    expect(await a).toEqual({ defaultModelId: 'm-1' });
    expect(await b).toEqual({ defaultModelId: 'm-1' });
    expect(await c).toEqual({ defaultModelId: 'm-1' });
  });

  it('reuses the settled read for a caller that arrives later', async () => {
    // The real shape of the bug: the two callers were ~280ms apart, so the
    // second one arrived after the first had already resolved. A single-flight
    // guard would not have caught it — only a memo does.
    const first = service.getSettings();
    expect(flushSettings({ defaultModelId: 'm-1' })).toBe(1);
    await first;

    const second = await service.getSettings();

    httpMock.expectNone(r => r.url === URL && r.method === 'GET');
    expect(second).toEqual({ defaultModelId: 'm-1' });
  });

  // ── Correctness guards on the memo ────────────────────────────────────

  it('retries after a failed read rather than caching the rejection', async () => {
    const failed = service.getSettings();
    const reqs = httpMock.match(r => r.url === URL && r.method === 'GET');
    expect(reqs.length).toBe(1);
    reqs[0].flush('boom', { status: 500, statusText: 'Server Error' });
    await expect(failed).rejects.toBeTruthy();

    // A cached rejection would strand every later caller on a failure they
    // had no way to see or clear.
    const retried = service.getSettings();
    expect(flushSettings({ defaultModelId: 'm-2' })).toBe(1);
    expect(await retried).toEqual({ defaultModelId: 'm-2' });
  });

  it('drops the memo after a write so the next read sees the new value', async () => {
    const initial = service.getSettings();
    expect(flushSettings({ defaultModelId: 'old' })).toBe(1);
    await initial;

    const update = service.updateSettings({ defaultModelId: 'new' });
    httpMock.expectOne(r => r.url === URL && r.method === 'PUT')
      .flush({ defaultModelId: 'new' });
    await update;

    // `updateSettings` also reloads `settingsResource`, which reads through
    // `getSettings` — so absorb whatever it issued alongside our own read.
    const next = service.getSettings();
    expect(flushSettings({ defaultModelId: 'new' })).toBeGreaterThan(0);
    expect(await next).toEqual({ defaultModelId: 'new' });
  });
});
