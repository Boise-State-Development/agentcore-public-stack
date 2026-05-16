import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ArtifactHttpService } from './artifact-http.service';
import { ConfigService } from '../../../services/config.service';

const API = 'http://localhost:8000';

describe('ArtifactHttpService', () => {
  let service: ArtifactHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ArtifactHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
      ],
    });
    service = TestBed.inject(ArtifactHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('mints a render token and maps expires_at', async () => {
    const p = service.mintRenderToken('art-1', 2, 'sess-9');
    const req = httpMock.expectOne(`${API}/artifacts/art-1/render-token`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ version: 2, sessionId: 'sess-9' });
    req.flush({ url: 'https://artifacts.x/?t=jwt', expires_at: '2026-05-15T12:02:00+00:00' });
    await expect(p).resolves.toEqual({
      url: 'https://artifacts.x/?t=jwt',
      expiresAt: '2026-05-15T12:02:00+00:00',
    });
  });

  it('url-encodes the artifact id in the token path', async () => {
    const p = service.mintRenderToken('a/b 1', 1, 's');
    const req = httpMock.expectOne(`${API}/artifacts/a%2Fb%201/render-token`);
    req.flush({ url: 'u', expires_at: 'e' });
    await p;
  });

  it('lists session artifacts and normalizes snake_case to camelCase', async () => {
    const p = service.listSessionArtifacts('sess-9');
    const req = httpMock.expectOne(
      (r) => r.url === `${API}/artifacts` && r.params.get('session_id') === 'sess-9',
    );
    expect(req.request.method).toBe('GET');
    req.flush({
      artifacts: [
        {
          artifact_id: 'art-1',
          version: 3,
          title: 'Report',
          content_type: 'text/html; charset=utf-8',
          updated_at: '2026-05-15T12:00:00+00:00',
          created_at: '2026-05-15T10:00:00+00:00',
        },
      ],
    });
    await expect(p).resolves.toEqual([
      {
        artifactId: 'art-1',
        version: 3,
        title: 'Report',
        contentType: 'text/html; charset=utf-8',
        updatedAt: '2026-05-15T12:00:00+00:00',
        createdAt: '2026-05-15T10:00:00+00:00',
      },
    ]);
  });

  it('tolerates a missing artifacts array', async () => {
    const p = service.listSessionArtifacts('sess-9');
    httpMock
      .expectOne((r) => r.url === `${API}/artifacts`)
      .flush({});
    await expect(p).resolves.toEqual([]);
  });
});
