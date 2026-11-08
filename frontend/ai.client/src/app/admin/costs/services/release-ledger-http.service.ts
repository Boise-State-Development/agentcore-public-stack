import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  LedgerQualityResponse,
  LedgerRangeOptions,
  LedgerReadout,
  LedgerReadoutOptions,
  LedgerSeriesResponse,
  LedgerToolsResponse,
  LedgerUnitKind,
  LedgerVersionsResponse,
} from '../models';

/**
 * HTTP client for the release ledger (`/admin/costs/ledger/*`, docs/specs/release-ledger.md §6.1).
 *
 * Every route is `admin.costs` scoped and 404s while `RELEASE_LEDGER_ENABLED`
 * is off; the Releases tab is only offered while `features.releaseLedger` is on.
 * Nothing here computes a KPI or a delta: the readout is the one place the
 * comparison lives (D10), and the tab renders what it returns.
 */
@Injectable({ providedIn: 'root' })
export class ReleaseLedgerHttpService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/costs/ledger`);

  getVersions(range: LedgerRangeOptions = {}): Observable<LedgerVersionsResponse> {
    return this.http.get<LedgerVersionsResponse>(`${this.baseUrl()}/versions`, {
      params: rangeParams(range),
    });
  }

  getSeries(
    range: LedgerRangeOptions,
    unit: LedgerUnitKind,
    limit = 50,
  ): Observable<LedgerSeriesResponse> {
    const params = rangeParams(range).set('by', 'version').set('unit', unit).set('limit', limit);
    return this.http.get<LedgerSeriesResponse>(`${this.baseUrl()}/series`, { params });
  }

  getTools(range: LedgerRangeOptions, version?: string): Observable<LedgerToolsResponse> {
    let params = rangeParams(range);
    if (version) params = params.set('version', version);
    return this.http.get<LedgerToolsResponse>(`${this.baseUrl()}/tools`, { params });
  }

  getQuality(range: LedgerRangeOptions, unit: LedgerUnitKind): Observable<LedgerQualityResponse> {
    const params = rangeParams(range).set('unit', unit);
    return this.http.get<LedgerQualityResponse>(`${this.baseUrl()}/quality`, { params });
  }

  getReadout(options: LedgerReadoutOptions = {}): Observable<LedgerReadout> {
    let params = new HttpParams();
    if (options.version) params = params.set('version', options.version);
    if (options.baseline) params = params.set('baseline', options.baseline);
    if (options.days !== undefined) params = params.set('days', options.days);
    return this.http.get<LedgerReadout>(`${this.baseUrl()}/readout`, { params });
  }

  /** The raw ledger rows for the range, as NDJSON (ids and numbers only). */
  exportNdjson(range: LedgerRangeOptions): Observable<Blob> {
    return this.http.get(`${this.baseUrl()}/export`, {
      params: rangeParams(range),
      responseType: 'blob',
    });
  }
}

function rangeParams(range: LedgerRangeOptions): HttpParams {
  let params = new HttpParams();
  if (range.from) params = params.set('from', range.from);
  if (range.to) params = params.set('to', range.to);
  return params;
}
