import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import { RunNowRequest, RunNowResponse } from '../models/schedule.model';

/**
 * Thin HTTP layer over `/runs/*` (app-api, cookie-authed). The "Run now"
 * surface fires one attended agent turn through the exact headless machinery
 * a scheduled run will use — it does not create a saved schedule. Synchronous
 * from the caller's perspective (the response is the full run result once the
 * turn drains, bounded by the harness's 300s budget).
 */
@Injectable({
  providedIn: 'root',
})
export class RunApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/runs`);

  runNow(request: RunNowRequest): Observable<RunNowResponse> {
    return this.http.post<RunNowResponse>(`${this.baseUrl()}/now`, request);
  }
}
