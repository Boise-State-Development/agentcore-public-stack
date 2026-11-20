import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { RunNowRequest, RunStartedResponse, RunStatusResponse } from '../models/schedule.model';

/**
 * Thin HTTP layer over `/runs/*` (app-api, cookie-authed). The "Run now"
 * surface fires one attended agent turn through the exact headless machinery
 * a scheduled run will use — it does not create a saved schedule. POST
 * `/runs/now` returns as soon as the run starts; `getRun` reads its outcome.
 */
@Injectable({
  providedIn: 'root',
})
export class RunApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/runs`);

  runNow(request: RunNowRequest): Observable<RunStartedResponse> {
    return this.http.post<RunStartedResponse>(`${this.baseUrl()}/now`, request);
  }

  /**
   * One poll of a run's status. No global error toast: a failed poll is
   * retried by the caller, and the run itself may well be fine.
   */
  getRun(runId: string): Observable<RunStatusResponse> {
    return this.http.get<RunStatusResponse>(`${this.baseUrl()}/${encodeURIComponent(runId)}`, {
      context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true),
    });
  }
}
