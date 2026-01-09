import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { 
  Assistant, 
  CreateAssistantDraftRequest,
  CreateAssistantRequest, 
  UpdateAssistantRequest,
  AssistantsListResponse 
} from '../models/assistant.model';
import {
  CreateDocumentRequest,
  UploadUrlResponse
} from '../models/document.model';
import { environment } from '../../../environments/environment';

@Injectable({
  providedIn: 'root'
})
export class AssistantApiService {
  private http = inject(HttpClient);
  private readonly baseUrl = `${environment.appApiUrl}/assistants`;

  createDraft(request: CreateAssistantDraftRequest = {}): Observable<Assistant> {
    return this.http.post<Assistant>(`${this.baseUrl}/draft`, request);
  }

  createAssistant(request: CreateAssistantRequest): Observable<Assistant> {
    return this.http.post<Assistant>(this.baseUrl, request);
  }

  getAssistants(params?: {
    limit?: number;
    nextToken?: string;
    includeArchived?: boolean;
    includeDrafts?: boolean;
    includePublic?: boolean;
  }): Observable<AssistantsListResponse> {
    let httpParams = new HttpParams();
    if (params?.limit) {
      httpParams = httpParams.set('limit', params.limit.toString());
    }
    if (params?.nextToken) {
      httpParams = httpParams.set('next_token', params.nextToken);
    }
    if (params?.includeArchived !== undefined) {
      httpParams = httpParams.set('include_archived', params.includeArchived.toString());
    }
    if (params?.includeDrafts !== undefined) {
      httpParams = httpParams.set('include_drafts', params.includeDrafts.toString());
    }
    if (params?.includePublic !== undefined) {
      httpParams = httpParams.set('include_public', params.includePublic.toString());
    }

    return this.http.get<AssistantsListResponse>(this.baseUrl, { params: httpParams });
  }

  getAssistant(id: string): Observable<Assistant> {
    return this.http.get<Assistant>(`${this.baseUrl}/${id}`);
  }

  updateAssistant(id: string, request: UpdateAssistantRequest): Observable<Assistant> {
    return this.http.put<Assistant>(`${this.baseUrl}/${id}`, request);
  }

  archiveAssistant(id: string): Observable<Assistant> {
    return this.http.post<Assistant>(`${this.baseUrl}/${id}/archive`, {});
  }

  deleteAssistant(id: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/${id}`);
  }

  /**
   * Request a presigned URL for uploading a document to an assistant.
   *
   * @param assistantId - The assistant identifier
   * @param request - Document upload request with filename, contentType, and sizeBytes
   * @returns Observable of upload URL response
   */
  requestDocumentUploadUrl(
    assistantId: string,
    request: CreateDocumentRequest
  ): Observable<UploadUrlResponse> {
    return this.http.post<UploadUrlResponse>(
      `${this.baseUrl}/${assistantId}/documents/upload-url`,
      request
    );
  }
}
