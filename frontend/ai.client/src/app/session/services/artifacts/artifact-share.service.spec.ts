import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { HttpContext } from '@angular/common/http';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ArtifactShareService } from './artifact-share.service';
import { ConfigService } from '../../../services/config.service';

const API = 'http://localhost:8000';

const SHARE = {
  shareId: 'share-1',
  artifactId: 'art-1',
  version: 2,
  ownerId: 'owner-1',
  accessLevel: 'public' as const,
  title: 'My Chart',
  contentType: 'text/html; charset=utf-8',
  createdAt: '2026-09-03T00:00:00+00:00',
  shareUrl: '/shared-artifact/share-1',
};

describe('ArtifactShareService', () => {
  let service: ArtifactShareService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ArtifactShareService,
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
      ],
    });
    service = TestBed.inject(ArtifactShareService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  // ----------------------------------------------------------------
  // Owner CRUD
  // ----------------------------------------------------------------

  it('creates a share pinned to an explicit version', async () => {
    const p = service.createShare('art-1', 2, 'public');
    const req = httpMock.expectOne(`${API}/artifacts/art-1/shares`);
    expect(req.request.method).toBe('POST');
    // The version is always sent explicitly — the backend never defaults
    // a share to HEAD.
    expect(req.request.body).toEqual({ version: 2, accessLevel: 'public' });
    req.flush(SHARE);
    await expect(p).resolves.toEqual(SHARE);
  });

  it('sends allowedEmails for a specific share', async () => {
    const p = service.createShare('art-1', 1, 'specific', ['a@x.com']);
    const req = httpMock.expectOne(`${API}/artifacts/art-1/shares`);
    expect(req.request.body).toEqual({
      version: 1,
      accessLevel: 'specific',
      allowedEmails: ['a@x.com'],
    });
    req.flush(SHARE);
    await p;
  });

  it('omits an empty allowedEmails rather than sending []', async () => {
    const p = service.createShare('art-1', 1, 'public', []);
    const req = httpMock.expectOne(`${API}/artifacts/art-1/shares`);
    expect(req.request.body).toEqual({ version: 1, accessLevel: 'public' });
    req.flush(SHARE);
    await p;
  });

  it('lists shares and defaults a missing array to empty', async () => {
    const p = service.listShares('art-1');
    const req = httpMock.expectOne(`${API}/artifacts/art-1/shares`);
    expect(req.request.method).toBe('GET');
    req.flush({});
    await expect(p).resolves.toEqual([]);
  });

  it('lists shares across versions of one artifact', async () => {
    const p = service.listShares('art-1');
    httpMock
      .expectOne(`${API}/artifacts/art-1/shares`)
      .flush({ shares: [SHARE, { ...SHARE, shareId: 'share-2', version: 1 }] });
    const shares = await p;
    expect(shares.map((s) => s.version)).toEqual([2, 1]);
  });

  it('patches only the fields that were supplied', async () => {
    const p = service.updateShare('share-1', 'specific', ['b@x.com']);
    const req = httpMock.expectOne(`${API}/artifacts/shares/share-1`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({
      accessLevel: 'specific',
      allowedEmails: ['b@x.com'],
    });
    req.flush(SHARE);
    await p;
  });

  it('patches with an empty body when nothing was supplied', async () => {
    const p = service.updateShare('share-1');
    const req = httpMock.expectOne(`${API}/artifacts/shares/share-1`);
    expect(req.request.body).toEqual({});
    req.flush(SHARE);
    await p;
  });

  it('revokes a share', async () => {
    const p = service.revokeShare('share-1');
    const req = httpMock.expectOne(`${API}/artifacts/shares/share-1`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });
    await p;
  });

  // ----------------------------------------------------------------
  // Recipient
  // ----------------------------------------------------------------

  it('reads recipient metadata', async () => {
    const p = service.getSharedArtifact('share-1');
    const req = httpMock.expectOne(`${API}/shared-artifacts/share-1`);
    expect(req.request.method).toBe('GET');
    req.flush({
      shareId: 'share-1',
      title: 'My Chart',
      contentType: 'text/html; charset=utf-8',
      version: 2,
      createdAt: '2026-09-03T00:00:00+00:00',
      ownerEmail: 'owner@x.com',
      canDownload: true,
    });
    await expect(p).resolves.toMatchObject({
      ownerEmail: 'owner@x.com',
      canDownload: true,
    });
  });

  it('mints a shared render token and maps expires_at', async () => {
    const p = service.mintSharedRenderToken('share-1');
    const req = httpMock.expectOne(
      `${API}/shared-artifacts/share-1/render-token`,
    );
    expect(req.request.method).toBe('POST');
    req.flush({
      url: 'https://artifacts.x/?t=jwt',
      expires_at: '2026-09-03T00:02:00+00:00',
    });
    // Same shape as the owner mint, so the iframe and the download
    // service work against either path unchanged.
    await expect(p).resolves.toEqual({
      url: 'https://artifacts.x/?t=jwt',
      expiresAt: '2026-09-03T00:02:00+00:00',
    });
  });

  // ----------------------------------------------------------------
  // Path safety
  // ----------------------------------------------------------------

  it('url-encodes ids in every path segment it builds', async () => {
    const create = service.createShare('a/b 1', 1, 'public');
    httpMock.expectOne(`${API}/artifacts/a%2Fb%201/shares`).flush(SHARE);
    await create;

    const list = service.listShares('a/b 1');
    httpMock.expectOne(`${API}/artifacts/a%2Fb%201/shares`).flush({ shares: [] });
    await list;

    const revoke = service.revokeShare('s/1');
    httpMock
      .expectOne(`${API}/artifacts/shares/s%2F1`)
      .flush(null, { status: 204, statusText: 'No Content' });
    await revoke;

    const mint = service.mintSharedRenderToken('s/1');
    httpMock
      .expectOne(`${API}/shared-artifacts/s%2F1/render-token`)
      .flush({ url: 'u', expires_at: 'e' });
    await mint;
  });

  // ----------------------------------------------------------------
  // Global error toast
  // ----------------------------------------------------------------

  it('opts calls with inline error UI out of the global toast', async () => {
    // The share modal and the recipient page render their own error
    // messages; without this a dead link shows the page's "Artifact not
    // found" AND a generic toast repeating the backend detail.
    const cases: Array<[string, () => Promise<unknown>, string]> = [
      ['create', () => service.createShare('art-1', 1, 'public'), `${API}/artifacts/art-1/shares`],
      ['update', () => service.updateShare('s1'), `${API}/artifacts/shares/s1`],
      ['revoke', () => service.revokeShare('s1'), `${API}/artifacts/shares/s1`],
      ['metadata', () => service.getSharedArtifact('s1'), `${API}/shared-artifacts/s1`],
      ['mint', () => service.mintSharedRenderToken('s1'), `${API}/shared-artifacts/s1/render-token`],
      ['content', () => service.getSharedArtifactContent('s1'), `${API}/shared-artifacts/s1/content`],
    ];

    for (const [label, call, url] of cases) {
      const p = call();
      const req = httpMock.expectOne(url);
      expect(
        req.request.context.get(SUPPRESS_ERROR_TOAST),
        `${label} should suppress the global toast`,
      ).toBe(true);
      req.flush({ url: 'u', expires_at: 'e', content: '', content_type: '', version: 1 });
      await p;
    }
  });

  describe('listSharedWithMe', () => {
    it('returns null when the inbox does not exist in this environment', async () => {
      // A 404 is the backend saying ARTIFACT_SHARE_INBOX_ENABLED is off.
      // Null and an empty page are different facts and the library page
      // renders them differently — null hides the tabs, [] says
      // "nothing yet".
      const p = service.listSharedWithMe();
      httpMock
        .expectOne((r) => r.url === `${API}/shared-artifacts`)
        .flush(null, { status: 404, statusText: 'Not Found' });

      await expect(p).resolves.toBeNull();
    });

    it('rethrows anything that is not a 404', async () => {
      // "We could not load your inbox" is not "you do not have one" —
      // swallowing a 503 would silently hide a tab that exists.
      const p = service.listSharedWithMe();
      httpMock
        .expectOne((r) => r.url === `${API}/shared-artifacts`)
        .flush(null, { status: 503, statusText: 'Service Unavailable' });

      await expect(p).rejects.toBeTruthy();
    });

    it('passes the cursor through and normalizes a missing one', async () => {
      const p = service.listSharedWithMe('cursor-1');
      const req = httpMock.expectOne(
        (r) => r.url === `${API}/shared-artifacts`,
      );
      expect(req.request.params.get('cursor')).toBe('cursor-1');
      req.flush({ artifacts: [] });

      // The backend omits `nextCursor` on the last page; the client
      // normalizes it so callers can page until it is null.
      await expect(p).resolves.toEqual({ artifacts: [], nextCursor: null });
    });
  });

  it('leaves the toast on for the share list, which degrades silently', async () => {
    // listShares has no inline error UI — the dialog still opens and can
    // create a link — so the toast is the only signal the list is stale.
    const p = service.listShares('art-1');
    const req = httpMock.expectOne(`${API}/artifacts/art-1/shares`);
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(false);
    req.flush({ shares: [] });
    await p;
  });
});
