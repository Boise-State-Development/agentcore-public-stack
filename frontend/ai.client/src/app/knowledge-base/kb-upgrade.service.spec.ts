import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { KbUpgradeService, UpgradeStatus } from './kb-upgrade.service';
import { ConfigService } from '../services/config.service';

const ENTITY = 'ast-1';
const BASE = 'http://api.test/assistants/ast-1/knowledge-base/upgrade';

/**
 * The upgrade surface, whose defining property is that it fails soft.
 *
 * This card is decoration on a page whose real job — uploading and listing
 * documents — works regardless. Every assertion about failure here exists so a
 * broken or absent upgrade endpoint cannot take that page down with it.
 */
describe('KbUpgradeService', () => {
  let service: KbUpgradeService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ConfigService,
          useValue: { appApiUrl: () => 'http://api.test' },
        },
      ],
    });
    service = TestBed.inject(KbUpgradeService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('getStatus', () => {
    it('reads the status from the knowledge-base upgrade endpoint', async () => {
      const pending = service.getStatus(ENTITY);
      const request = http.expectOne(BASE);
      expect(request.request.method).toBe('GET');
      request.flush({
        phase: 'available',
        canUpgrade: true,
        progress: { completed: 0, total: 3, skipped: 1 },
        reason: null,
        noticePending: false,
        documentsNotCarried: [],
      } satisfies UpgradeStatus);

      const status = await pending;
      expect(status.phase).toBe('available');
      expect(status.progress?.total).toBe(3);
    });

    it('resolves to "nothing to show" when the request fails', async () => {
      const pending = service.getStatus(ENTITY);
      http.expectOne(BASE).flush('boom', { status: 500, statusText: 'Server Error' });

      const status = await pending;
      expect(status.phase).toBe('none');
      expect(status.canUpgrade).toBe(false);
      expect(status.documentsNotCarried).toEqual([]);
    });

    it('never leaves documentsNotCarried undefined on a partial payload', async () => {
      // A field the server omits must not become `undefined` on the way to a
      // template that calls `.length` on it.
      const pending = service.getStatus(ENTITY);
      http.expectOne(BASE).flush({ phase: 'available', canUpgrade: true });

      const status = await pending;
      expect(status.documentsNotCarried).toEqual([]);
      expect(status.noticePending).toBe(false);
    });
  });

  describe('start and retry', () => {
    it('posts to the upgrade endpoint to start', async () => {
      const pending = service.start(ENTITY);
      const request = http.expectOne(BASE);
      expect(request.request.method).toBe('POST');
      request.flush({ phase: 'in_progress', started: true, message: 'Upgrade started.' });

      expect((await pending).started).toBe(true);
    });

    it('posts to the retry endpoint to restart', async () => {
      const pending = service.retry(ENTITY);
      const request = http.expectOne(`${BASE}/retry`);
      expect(request.request.method).toBe('POST');
      request.flush({ phase: 'in_progress', started: true, message: 'Upgrade restarted.' });

      expect((await pending).started).toBe(true);
    });

    it("rejects with the server's own message so the caller can show it", async () => {
      const pending = service.start(ENTITY);
      http
        .expectOne(BASE)
        .flush(
          { detail: 'Upgrades are not being accepted at the moment.' },
          { status: 409, statusText: 'Conflict' },
        );

      await expect(pending).rejects.toThrow('Upgrades are not being accepted at the moment.');
    });

    it('falls back to actionable copy that never mentions a status code', async () => {
      const pending = service.start(ENTITY);
      http.expectOne(BASE).flush(null, { status: 500, statusText: 'Server Error' });

      await expect(pending).rejects.toThrow(/Nothing has changed/);
      await pending.catch((err: Error) => {
        expect(err.message).not.toMatch(/\b500\b/);
      });
    });
  });

  describe('dismissNotice', () => {
    it('posts to the notice endpoint', async () => {
      const pending = service.dismissNotice(ENTITY);
      const request = http.expectOne(`${BASE}/notice`);
      expect(request.request.method).toBe('POST');
      request.flush(null, { status: 204, statusText: 'No Content' });
      await expect(pending).resolves.toBeUndefined();
    });

    it('swallows failure, because the notice is already hidden locally', async () => {
      // An error toast reading "we could not forget something" is pure noise;
      // the worst real consequence is the notice returning on the next load.
      const pending = service.dismissNotice(ENTITY);
      http.expectOne(`${BASE}/notice`).flush(null, { status: 500, statusText: 'Server Error' });
      await expect(pending).resolves.toBeUndefined();
    });
  });

  it('never says "vector" in any message it can produce', async () => {
    // Requirement 23.6. The service authors only the fallback strings; the rest
    // come from the server, which has its own sweep over the same rule.
    const pending = service.start(ENTITY);
    http.expectOne(BASE).flush(null, { status: 500, statusText: 'Server Error' });
    await pending.catch((err: Error) => {
      expect(err.message.toLowerCase()).not.toContain('vector');
    });
  });
});
