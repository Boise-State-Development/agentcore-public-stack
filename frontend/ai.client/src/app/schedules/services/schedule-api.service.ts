import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  CreateScheduleRequest,
  ScheduledPrompt,
  ScheduleRunsResponse,
  ScheduledPromptsListResponse,
  UpdateScheduleRequest,
  WatchStateResponse,
} from '../models/schedule.model';

/**
 * Thin HTTP layer over `/schedules/*` (app-api, cookie-authed). Mirrors
 * `AssistantApiService` — no state, just the wire calls.
 */
@Injectable({
  providedIn: 'root',
})
export class ScheduleApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/schedules`);

  list(): Observable<ScheduledPromptsListResponse> {
    return this.http.get<ScheduledPromptsListResponse>(this.baseUrl());
  }

  get(scheduleId: string): Observable<ScheduledPrompt> {
    return this.http.get<ScheduledPrompt>(`${this.baseUrl()}/${scheduleId}`);
  }

  create(request: CreateScheduleRequest): Observable<ScheduledPrompt> {
    return this.http.post<ScheduledPrompt>(this.baseUrl(), request);
  }

  update(scheduleId: string, request: UpdateScheduleRequest): Observable<ScheduledPrompt> {
    return this.http.patch<ScheduledPrompt>(`${this.baseUrl()}/${scheduleId}`, request);
  }

  pause(scheduleId: string): Observable<ScheduledPrompt> {
    return this.http.post<ScheduledPrompt>(`${this.baseUrl()}/${scheduleId}/pause`, {});
  }

  resume(scheduleId: string): Observable<ScheduledPrompt> {
    return this.http.post<ScheduledPrompt>(`${this.baseUrl()}/${scheduleId}/resume`, {});
  }

  delete(scheduleId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl()}/${scheduleId}`);
  }

  /**
   * A personal schedule's runs, newest first (quiet-watcher §3.4). 404s while
   * the backend's PROACTIVE_RUNS_ENABLED is off, so only call it behind
   * `features.proactiveRuns`.
   */
  runs(scheduleId: string): Observable<ScheduleRunsResponse> {
    return this.http.get<ScheduleRunsResponse>(`${this.baseUrl()}/${scheduleId}/runs`);
  }

  /** A watcher's state, or `{state: null}` when it has none. Behind the same flag as `runs`. */
  watchState(scheduleId: string): Observable<WatchStateResponse> {
    return this.http.get<WatchStateResponse>(`${this.baseUrl()}/${scheduleId}/state`);
  }

  /** Start over: clears the watcher's state, so its next run reads everything as new. */
  clearWatchState(scheduleId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl()}/${scheduleId}/state`);
  }
}
