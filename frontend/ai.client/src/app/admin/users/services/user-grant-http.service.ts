import { Injectable, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { UserGrant, UserGrantUpdate } from '../models';

/**
 * HTTP client for one user's direct grant (`/admin/user-grants/{userId}`).
 *
 * The surface is mounted only while the backend's `USER_GRANTS_ENABLED` is
 * on; with it off every call 404s, and the Access section renders that as
 * "not available in this environment" rather than as a broken page.
 */
@Injectable({
  providedIn: 'root',
})
export class UserGrantHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private baseUrl = computed(() => `${this.config.appApiUrl()}/admin/user-grants`);

  /** The user's grant, or the empty shape when they have none. */
  getGrant(userId: string): Observable<UserGrant> {
    return this.http.get<UserGrant>(`${this.baseUrl()}/${encodeURIComponent(userId)}`);
  }

  /** Full replace. Three empty lists remove the grant; the empty shape comes back. */
  setGrant(userId: string, update: UserGrantUpdate): Observable<UserGrant> {
    return this.http.put<UserGrant>(`${this.baseUrl()}/${encodeURIComponent(userId)}`, update);
  }

  deleteGrant(userId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl()}/${encodeURIComponent(userId)}`);
  }
}
