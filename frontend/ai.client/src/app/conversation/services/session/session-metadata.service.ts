import { inject, Injectable } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { SessionMetadata, UpdateSessionMetadataRequest } from '../models/session-metadata.model';
import { AuthService } from '../../../auth/auth.service';

@Injectable({
  providedIn: 'root'
})
export class SessionMetadataService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Get session metadata for a specific session
   */
  getSessionMetadata(sessionId: string): Observable<SessionMetadata> {
    const headers = this.getAuthHeaders();
    return this.http.get<SessionMetadata>(
      `${environment.apiUrl}/sessions/${sessionId}/metadata`,
      { headers }
    );
  }

  /**
   * Update session metadata
   * Performs a deep merge - only updates fields that are provided
   */
  updateSessionMetadata(
    sessionId: string,
    updates: UpdateSessionMetadataRequest
  ): Observable<SessionMetadata> {
    const headers = this.getAuthHeaders();
    return this.http.put<SessionMetadata>(
      `${environment.apiUrl}/sessions/${sessionId}/metadata`,
      updates,
      { headers }
    );
  }

  /**
   * Update session title
   */
  updateSessionTitle(sessionId: string, title: string): Observable<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { title });
  }

  /**
   * Toggle starred status
   */
  toggleStarred(sessionId: string, starred: boolean): Observable<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { starred });
  }

  /**
   * Update session tags
   */
  updateSessionTags(sessionId: string, tags: string[]): Observable<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { tags });
  }

  /**
   * Update session status
   */
  updateSessionStatus(
    sessionId: string,
    status: 'active' | 'archived' | 'deleted'
  ): Observable<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { status });
  }

  /**
   * Update session preferences
   */
  updateSessionPreferences(
    sessionId: string,
    preferences: {
      lastModel?: string;
      lastTemperature?: number;
      enabledTools?: string[];
      selectedPromptId?: string;
      customPromptText?: string;
    }
  ): Observable<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, preferences);
  }

  /**
   * Helper to get auth headers
   */
  private getAuthHeaders(): HttpHeaders {
    const token = this.authService.getAccessToken();
    let headers = new HttpHeaders({
      'Content-Type': 'application/json'
    });

    if (token) {
      headers = headers.set('Authorization', `Bearer ${token}`);
    }

    return headers;
  }
}
