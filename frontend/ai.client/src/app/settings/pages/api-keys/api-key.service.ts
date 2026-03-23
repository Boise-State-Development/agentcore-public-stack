import { Injectable, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, map } from 'rxjs';
import { ConfigService } from '../../../services/config.service';

export interface ApiKey {
  key_id: string;
  name: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
}

export interface CreateApiKeyResponse {
  key_id: string;
  name: string;
  key: string;
  created_at: string;
  expires_at: string;
}

interface GetApiKeyResponse {
  key: ApiKey | null;
}

interface DeleteApiKeyResponse {
  key_id: string;
  deleted: boolean;
}

@Injectable({
  providedIn: 'root',
})
export class ApiKeyService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/auth/api-keys`);

  /**
   * Get the current user's API key (one key per user).
   */
  getKey(): Observable<ApiKey | null> {
    return this.http
      .get<GetApiKeyResponse>(this.baseUrl())
      .pipe(map((res: GetApiKeyResponse) => res.key));
  }

  /**
   * Create a new API key. Replaces any existing key.
   */
  createKey(name: string): Observable<CreateApiKeyResponse> {
    return this.http.post<CreateApiKeyResponse>(this.baseUrl(), { name });
  }

  /**
   * Delete the current API key.
   */
  deleteKey(keyId: string): Observable<DeleteApiKeyResponse> {
    return this.http.delete<DeleteApiKeyResponse>(`${this.baseUrl()}/${keyId}`);
  }
}
