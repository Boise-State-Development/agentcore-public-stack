import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  UserListResponse,
  UserDetailResponse,
  UserListRequestOptions,
  UserSearchOptions,
} from '../models';

/**
 * HTTP service for admin user management API.
 * Communicates with FastAPI backend admin/users endpoints.
 */
@Injectable({
  providedIn: 'root',
})
export class UserHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private baseUrl = computed(() => `${this.config.appApiUrl()}/admin/users`);

  /**
   * List users with optional filters and pagination.
   *
   * @param options Filter and pagination options
   * @returns Observable of paginated user list
   */
  listUsers(options: UserListRequestOptions = {}): Observable<UserListResponse> {
    return this.http.get<UserListResponse>(this.baseUrl(), { params: this.listParams(options) });
  }

  /**
   * Search users by part of a name, email or user id. Case-insensitive and
   * typo tolerant, ranked best match first, paged like `listUsers`. Every
   * profile for an exact email comes first, the live one (most recent login)
   * on top.
   *
   * @param options Query plus the list filters and pagination
   */
  searchUsers(options: UserSearchOptions): Observable<UserListResponse> {
    const params = this.listParams(options).set('q', options.q);
    return this.http.get<UserListResponse>(`${this.baseUrl()}/search`, { params });
  }

  private listParams(options: UserListRequestOptions): HttpParams {
    let params = new HttpParams();

    if (options.status) {
      params = params.set('status', options.status);
    }
    if (options.domain) {
      params = params.set('domain', options.domain);
    }
    if (options.limit !== undefined) {
      params = params.set('limit', options.limit.toString());
    }
    if (options.cursor) {
      params = params.set('cursor', options.cursor);
    }
    return params;
  }

  /**
   * Get comprehensive user detail.
   * Includes profile, cost summary, quota status, and recent events.
   *
   * @param userId User identifier
   * @returns Observable of user detail
   */
  getUserDetail(userId: string): Observable<UserDetailResponse> {
    return this.http.get<UserDetailResponse>(`${this.baseUrl()}/${encodeURIComponent(userId)}`);
  }

  /**
   * Distinct email domains, the most users first.
   *
   * @param limit Maximum number of domains to return
   * @returns Observable of domain strings
   */
  listDomains(limit: number = 50): Observable<string[]> {
    const params = new HttpParams().set('limit', limit.toString());
    return this.http.get<string[]>(`${this.baseUrl()}/domains/list`, { params });
  }
}
