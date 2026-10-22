import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { FleetFeedbackResponse } from '../fleet-feedback.models';

/** HTTP for the admin feedback surface (`/admin/feedback`). */
@Injectable({ providedIn: 'root' })
export class AdminFeedbackHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private baseUrl = computed(() => `${this.config.appApiUrl()}/admin/feedback`);

  /** Down-thumb rate by config arm across the fleet, over a trailing window. */
  getFleetFeedback(days: number): Observable<FleetFeedbackResponse> {
    return this.http.get<FleetFeedbackResponse>(`${this.baseUrl()}/fleet`, {
      params: new HttpParams().set('days', String(days)),
    });
  }
}
