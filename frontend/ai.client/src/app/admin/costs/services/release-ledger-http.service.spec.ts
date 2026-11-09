import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ReleaseLedgerHttpService } from './release-ledger-http.service';
import { ConfigService } from '../../../services/config.service';

describe('ReleaseLedgerHttpService', () => {
  let service: ReleaseLedgerHttpService;
  let http: HttpTestingController;
  const base = 'http://localhost:8000/admin/costs/ledger';

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ReleaseLedgerHttpService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  it('reads the version timeline for a range', () => {
    service.getVersions({ from: '2026-10-09', to: '2026-11-07' }).subscribe();
    const req = http.expectOne(`${base}/versions?from=2026-10-09&to=2026-11-07`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('asks for the version series at the chosen granularity', () => {
    service.getSeries({ from: '2026-10-09', to: '2026-11-07' }, 'build').subscribe();
    http.expectOne(`${base}/series?from=2026-10-09&to=2026-11-07&by=version&unit=build&limit=50`).flush({});
  });

  it('sends a build unit as <appVersion>#<build>, encoded', () => {
    service.getReadout({ version: '1.33.0#d5ead33e', baseline: '1.33.0#f200a6a3', days: 7 }).subscribe();
    const req = http.expectOne(r => r.url === `${base}/readout`);
    expect(req.request.params.get('version')).toBe('1.33.0#d5ead33e');
    expect(req.request.urlWithParams).toContain('version=1.33.0%23d5ead33e');
    expect(req.request.params.get('baseline')).toBe('1.33.0#f200a6a3');
    expect(req.request.params.get('days')).toBe('7');
    req.flush({});
  });

  it('reads the eval runs for a range, sending only the filters chosen', () => {
    service.getEvals({ from: '2026-10-10', to: '2026-11-08', modelId: 'us.anthropic.claude-haiku-5-5', unit: 'build' }).subscribe();
    const req = http.expectOne(r => r.url === `${base}/evals`);
    expect(req.request.params.get('from')).toBe('2026-10-10');
    expect(req.request.params.get('modelId')).toBe('us.anthropic.claude-haiku-5-5');
    expect(req.request.params.get('unit')).toBe('build');
    expect(req.request.params.has('track')).toBe(false);
    req.flush({});
  });

  it('lets the endpoint pick the comparison when nothing is chosen', () => {
    service.getReadout().subscribe();
    http.expectOne(`${base}/readout`).flush({});
  });

  it('scopes the tools table and the quality runs', () => {
    service.getTools({ from: '2026-11-07', to: '2026-11-07' }, '1.33.0').subscribe();
    http.expectOne(`${base}/tools?from=2026-11-07&to=2026-11-07&version=1.33.0`).flush({});
    service.getQuality({ from: '2026-10-09', to: '2026-11-07' }, 'version').subscribe();
    http.expectOne(`${base}/quality?from=2026-10-09&to=2026-11-07&unit=version`).flush({});
  });

  it('downloads the NDJSON export as a blob', () => {
    service.exportNdjson({ from: '2026-10-09', to: '2026-11-07' }).subscribe();
    const req = http.expectOne(`${base}/export?from=2026-10-09&to=2026-11-07`);
    expect(req.request.responseType).toBe('blob');
    req.flush(new Blob(['{}\n']));
  });
});
