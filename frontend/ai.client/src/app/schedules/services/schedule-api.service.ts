import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  CreateScheduleRequest,
  ScheduledPrompt,
  ScheduledPromptsListResponse,
  UpdateScheduleRequest,
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
}
