import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpContext, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../../services/config.service';
import {
  AddMembersResponse,
  BindingRef,
  BindingsResponse,
  CreateProjectRequest,
  DirectoryResponse,
  InstructionsResponse,
  MemberRole,
  MembersResponse,
  ModelConfig,
  ModelResponse,
  Project,
  ProjectDocument,
  ProjectDocumentsResponse,
  ProjectListResponse,
  ProjectMember,
  ProjectTasksResponse,
  ProjectUploadUrlResponse,
  SettingsVersion,
  SettingsVersionsResponse,
  SharedTasksResponse,
  UpdateProjectRequest,
} from '../models/project.model';
import { CreateDocumentRequest, DownloadUrlResponse } from '../../assistants/models/document.model';

/**
 * HTTP surface for `/projects` (shared-projects §5).
 *
 * Every call opts out of the global error toast. While `PROJECTS_ENABLED` is off the
 * whole surface 404s on purpose, and the pages turn that into a "not available"
 * state; every other failure is shown inline where it happened, so a toast would
 * only repeat it.
 */
@Injectable({ providedIn: 'root' })
export class ProjectApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/projects`);

  private options(params?: HttpParams) {
    return { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true), ...(params ? { params } : {}) };
  }

  private url(projectId: string, suffix = ''): string {
    return `${this.baseUrl()}/${encodeURIComponent(projectId)}${suffix}`;
  }

  // ---- projects ---------------------------------------------------------

  list(includeArchived = false): Observable<ProjectListResponse> {
    const params = new HttpParams().set('includeArchived', includeArchived);
    return this.http.get<ProjectListResponse>(this.baseUrl(), this.options(params));
  }

  create(request: CreateProjectRequest): Observable<Project> {
    return this.http.post<Project>(this.baseUrl(), request, this.options());
  }

  get(projectId: string): Observable<Project> {
    return this.http.get<Project>(this.url(projectId), this.options());
  }

  update(projectId: string, request: UpdateProjectRequest): Observable<Project> {
    return this.http.patch<Project>(this.url(projectId), request, this.options());
  }

  /** Purge an archived project (owner). */
  delete(projectId: string): Observable<void> {
    return this.http.delete<void>(this.url(projectId), this.options());
  }

  transfer(projectId: string, email: string): Observable<Project> {
    return this.http.post<Project>(this.url(projectId, '/transfer'), { email }, this.options());
  }

  // ---- members ----------------------------------------------------------

  members(projectId: string): Observable<MembersResponse> {
    return this.http.get<MembersResponse>(this.url(projectId, '/members'), this.options());
  }

  addMembers(projectId: string, emails: string[], role: MemberRole): Observable<AddMembersResponse> {
    return this.http.post<AddMembersResponse>(this.url(projectId, '/members'), { emails, role }, this.options());
  }

  updateMember(projectId: string, email: string, role: MemberRole): Observable<ProjectMember> {
    return this.http.patch<ProjectMember>(
      this.url(projectId, `/members/${encodeURIComponent(email)}`),
      { role },
      this.options(),
    );
  }

  removeMember(projectId: string, email: string): Observable<void> {
    return this.http.delete<void>(this.url(projectId, `/members/${encodeURIComponent(email)}`), this.options());
  }

  leave(projectId: string): Observable<void> {
    return this.http.delete<void>(this.url(projectId, '/members/me'), this.options());
  }

  directory(projectId: string, q: string, limit = 10): Observable<DirectoryResponse> {
    const params = new HttpParams().set('q', q).set('limit', limit);
    return this.http.get<DirectoryResponse>(this.url(projectId, '/directory'), this.options(params));
  }

  // ---- settings ---------------------------------------------------------

  instructions(projectId: string): Observable<InstructionsResponse> {
    return this.http.get<InstructionsResponse>(this.url(projectId, '/instructions'), this.options());
  }

  saveInstructions(projectId: string, instructions: string): Observable<InstructionsResponse> {
    return this.http.put<InstructionsResponse>(this.url(projectId, '/instructions'), { instructions }, this.options());
  }

  model(projectId: string): Observable<ModelResponse> {
    return this.http.get<ModelResponse>(this.url(projectId, '/model'), this.options());
  }

  saveModel(projectId: string, modelConfig: ModelConfig): Observable<ModelResponse> {
    return this.http.put<ModelResponse>(this.url(projectId, '/model'), { modelConfig }, this.options());
  }

  bindings(projectId: string, kind: 'tools' | 'skills'): Observable<BindingsResponse> {
    return this.http.get<BindingsResponse>(this.url(projectId, `/${kind}`), this.options());
  }

  saveBindings(projectId: string, kind: 'tools' | 'skills', bindings: BindingRef[]): Observable<BindingsResponse> {
    return this.http.put<BindingsResponse>(this.url(projectId, `/${kind}`), { bindings }, this.options());
  }

  versions(projectId: string, limit = 50): Observable<SettingsVersionsResponse> {
    const params = new HttpParams().set('limit', limit);
    return this.http.get<SettingsVersionsResponse>(this.url(projectId, '/instructions/versions'), this.options(params));
  }

  version(projectId: string, number: number): Observable<SettingsVersion> {
    return this.http.get<SettingsVersion>(this.url(projectId, `/instructions/versions/${number}`), this.options());
  }

  // ---- tasks ------------------------------------------------------------

  /** The caller's own tasks in the project, newest first. */
  tasks(projectId: string, limit = 20, nextToken?: string | null): Observable<ProjectTasksResponse> {
    let params = new HttpParams().set('limit', limit);
    if (nextToken) params = params.set('nextToken', nextToken);
    return this.http.get<ProjectTasksResponse>(this.url(projectId, '/tasks'), this.options(params));
  }

  /** Tasks members shared with the project, newest first. */
  sharedTasks(projectId: string): Observable<SharedTasksResponse> {
    return this.http.get<SharedTasksResponse>(this.url(projectId, '/shared-tasks'), this.options());
  }

  // ---- files ------------------------------------------------------------

  files(projectId: string, limit = 100, nextToken?: string | null): Observable<ProjectDocumentsResponse> {
    let params = new HttpParams().set('limit', limit);
    if (nextToken) params = params.set('nextToken', nextToken);
    return this.http.get<ProjectDocumentsResponse>(this.url(projectId, '/knowledge'), this.options(params));
  }

  file(projectId: string, documentId: string): Observable<ProjectDocument> {
    return this.http.get<ProjectDocument>(this.fileUrl(projectId, documentId), this.options());
  }

  fileDownloadUrl(projectId: string, documentId: string): Observable<DownloadUrlResponse> {
    return this.http.get<DownloadUrlResponse>(this.fileUrl(projectId, documentId, '/download'), this.options());
  }

  /** Start an upload (editor): a presigned S3 PUT plus the sharing notice. */
  fileUploadUrl(projectId: string, request: CreateDocumentRequest): Observable<ProjectUploadUrlResponse> {
    return this.http.post<ProjectUploadUrlResponse>(this.url(projectId, '/knowledge/upload-url'), request, this.options());
  }

  /** Mark an upload failed after the S3 PUT failed, so the file doesn't sit in `uploading`. */
  reportFileUploadFailure(projectId: string, documentId: string, error: string, details?: string): Observable<ProjectDocument> {
    return this.http.post<ProjectDocument>(
      this.fileUrl(projectId, documentId, '/upload-failed'),
      { error, details },
      this.options(),
    );
  }

  deleteFile(projectId: string, documentId: string): Observable<void> {
    return this.http.delete<void>(this.fileUrl(projectId, documentId), this.options());
  }

  private fileUrl(projectId: string, documentId: string, suffix = ''): string {
    return this.url(projectId, `/knowledge/${encodeURIComponent(documentId)}${suffix}`);
  }
}
