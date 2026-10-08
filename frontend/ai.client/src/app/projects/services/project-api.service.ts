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
  ProjectAuditResponse,
  ProjectListResponse,
  ProjectMember,
  ProjectTasksResponse,
  ProjectUploadUrlResponse,
  SettingsVersion,
  SettingsVersionsResponse,
  SharedTasksResponse,
  MaintenanceRun,
  MaintenanceRunsResponse,
  MemoryProposal,
  MemoryProposalDetail,
  MemoryProposalsResponse,
  MemoryArchiveResponse,
  MemoryEntriesResponse,
  MemoryFile,
  MemoryPinsResponse,
  MemoryRestoreResponse,
  CreateMemoryProposalRequest,
  MemoryFileHistory,
  MemoryFileVersionContent,
  SaveMemoryFileRequest,
  SaveMemoryFileResponse,
  MemoryScope,
  ProjectMemory,
  ProjectOutputsResponse,
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

  // ---- activity ---------------------------------------------------------

  /** The project's audit trail, newest first (editor). */
  audit(projectId: string, limit = 50, cursor?: string | null): Observable<ProjectAuditResponse> {
    let params = new HttpParams().set('limit', limit);
    if (cursor) params = params.set('cursor', cursor);
    return this.http.get<ProjectAuditResponse>(this.url(projectId, '/audit'), this.options(params));
  }

  // ---- tasks ------------------------------------------------------------

  /** The caller's own tasks in the project, newest first. */
  tasks(projectId: string, limit = 20, nextToken?: string | null): Observable<ProjectTasksResponse> {
    let params = new HttpParams().set('limit', limit);
    if (nextToken) params = params.set('nextToken', nextToken);
    return this.http.get<ProjectTasksResponse>(this.url(projectId, '/tasks'), this.options(params));
  }

  /** Tasks members shared with the project, newest first. */
  // ---- outputs (3.3) -----------------------------------------------------

  outputs(projectId: string): Observable<ProjectOutputsResponse> {
    return this.http.get<ProjectOutputsResponse>(this.url(projectId, '/outputs'), this.options());
  }

  /** Stop sharing an artifact with the project: revokes its project share. */
  removeOutput(projectId: string, artifactId: string): Observable<void> {
    return this.http.delete<void>(this.url(projectId, `/outputs/${encodeURIComponent(artifactId)}`), this.options());
  }

  // ---- memory proposals (2.5a) -------------------------------------------

  /** Editors and the owner get every proposal; anyone else gets their own. */
  proposals(projectId: string, state?: MemoryProposal['state']): Observable<MemoryProposalsResponse> {
    const params = state ? new HttpParams().set('state', state) : undefined;
    return this.http.get<MemoryProposalsResponse>(this.url(projectId, '/memory/proposals'), this.options(params));
  }

  proposal(projectId: string, proposalId: string): Observable<MemoryProposalDetail> {
    return this.http.get<MemoryProposalDetail>(
      this.url(projectId, `/memory/proposals/${encodeURIComponent(proposalId)}`),
      this.options(),
    );
  }

  /**
   * `text` is the reviewer's edited version; omit it to apply the proposal as written.
   * `ops` picks a maintenance proposal's changes by index; omit it to apply them all.
   */
  approveProposal(
    projectId: string,
    proposalId: string,
    body: { text?: string; note?: string; ops?: number[] },
  ): Observable<MemoryProposal> {
    return this.http.post<MemoryProposal>(
      this.url(projectId, `/memory/proposals/${encodeURIComponent(proposalId)}/approve`),
      body,
      this.options(),
    );
  }

  rejectProposal(projectId: string, proposalId: string, note?: string): Observable<MemoryProposal> {
    return this.http.post<MemoryProposal>(
      this.url(projectId, `/memory/proposals/${encodeURIComponent(proposalId)}/reject`),
      note ? { note } : {},
      this.options(),
    );
  }

  withdrawProposal(projectId: string, proposalId: string): Observable<MemoryProposal> {
    return this.http.post<MemoryProposal>(
      this.url(projectId, `/memory/proposals/${encodeURIComponent(proposalId)}/withdraw`),
      {},
      this.options(),
    );
  }

  // ---- maintenance (2.6a) -------------------------------------------------

  /** Owner or editor: queue a maintenance run on the shared memory, every file or one. 409 while one runs. */
  startMaintenance(projectId: string, slug?: string): Observable<MaintenanceRun> {
    return this.http.post<MaintenanceRun>(this.url(projectId, '/memory/maintenance'), slug ? { slug } : {}, this.options());
  }

  /** The project's recent runs, newest first (owner and editors). */
  maintenanceRuns(projectId: string): Observable<MaintenanceRunsResponse> {
    return this.http.get<MaintenanceRunsResponse>(this.url(projectId, '/memory/maintenance'), this.options());
  }

  maintenanceRun(projectId: string, runId: string): Observable<MaintenanceRun> {
    return this.http.get<MaintenanceRun>(
      this.url(projectId, `/memory/maintenance/${encodeURIComponent(runId)}`),
      this.options(),
    );
  }

  // ---- the Memory tab (2.8) ----------------------------------------------

  /** The project's shared space, the caller's own (null until they keep something) and the size limits. */
  memory(projectId: string): Observable<ProjectMemory> {
    return this.http.get<ProjectMemory>(this.url(projectId, '/memory'), this.options());
  }

  /** The caller's own memory in this project, created on first call. */
  createMyMemory(projectId: string): Observable<{ spaceId: string }> {
    return this.http.post<{ spaceId: string }>(this.url(projectId, '/memory/mine'), {}, this.options());
  }

  /** A space's files (its manifest). Project spaces take their roles from the project. */
  memoryEntries(spaceId: string): Observable<MemoryEntriesResponse> {
    return this.http.get<MemoryEntriesResponse>(this.spaceUrl(spaceId, '/entries'), this.options());
  }

  /** A space's MEMORY.md, the index that loads into every task. */
  memoryIndex(spaceId: string): Observable<{ content: string }> {
    return this.http.get<{ content: string }>(this.spaceUrl(spaceId, '/index'), this.options());
  }

  memoryFile(projectId: string, scope: MemoryScope, slug: string): Observable<MemoryFile> {
    return this.http.get<MemoryFile>(
      this.url(projectId, `/memory/files/${encodeSlug(slug)}`),
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  pinMemoryItem(projectId: string, scope: MemoryScope, slug: string, anchor: string): Observable<MemoryPinsResponse> {
    return this.http.post<MemoryPinsResponse>(this.url(projectId, '/memory/pins'), { scope, slug, anchor }, this.options());
  }

  unpinMemoryItem(projectId: string, scope: MemoryScope, slug: string, anchor: string): Observable<MemoryPinsResponse> {
    const params = new HttpParams().set('scope', scope).set('slug', slug).set('anchor', anchor);
    return this.http.delete<MemoryPinsResponse>(this.url(projectId, '/memory/pins'), this.options(params));
  }

  memoryArchive(projectId: string, scope: MemoryScope): Observable<MemoryArchiveResponse> {
    return this.http.get<MemoryArchiveResponse>(
      this.url(projectId, '/memory/archive'),
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  restoreMemoryItem(projectId: string, scope: MemoryScope, archiveId: string): Observable<MemoryRestoreResponse> {
    return this.http.post<MemoryRestoreResponse>(
      this.url(projectId, `/memory/archive/${encodeURIComponent(archiveId)}/restore`),
      {},
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  saveMemoryFile(projectId: string, scope: MemoryScope, slug: string, body: SaveMemoryFileRequest): Observable<SaveMemoryFileResponse> {
    return this.http.put<SaveMemoryFileResponse>(
      this.url(projectId, `/memory/files/${encodeSlug(slug)}`),
      body,
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  /** Its items go to the archive, and its line leaves the index. */
  deleteMemoryFile(projectId: string, scope: MemoryScope, slug: string): Observable<void> {
    return this.http.delete<void>(
      this.url(projectId, `/memory/files/${encodeSlug(slug)}`),
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  saveMemoryIndex(projectId: string, scope: MemoryScope, content: string): Observable<{ content: string }> {
    return this.http.put<{ content: string }>(
      this.url(projectId, '/memory/index'),
      { content },
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  /** Propose a change to the shared memory for an editor to review (any member). */
  proposeMemoryChange(projectId: string, body: CreateMemoryProposalRequest): Observable<MemoryProposal> {
    return this.http.post<MemoryProposal>(this.url(projectId, '/memory/proposals'), body, this.options());
  }

  /** A file's saved versions, newest first. */
  memoryHistory(spaceId: string, slug: string): Observable<MemoryFileHistory> {
    return this.http.get<MemoryFileHistory>(this.spaceUrl(spaceId, '/history'), this.options(new HttpParams().set('slug', slug)));
  }

  memoryVersion(spaceId: string, slug: string, version: number): Observable<MemoryFileVersionContent> {
    return this.http.get<MemoryFileVersionContent>(
      this.spaceUrl(spaceId, `/history/${version}`),
      this.options(new HttpParams().set('slug', slug)),
    );
  }

  /** Make an earlier version current again, as a new version. */
  restoreMemoryVersion(projectId: string, scope: MemoryScope, slug: string, version: number): Observable<MemoryRestoreResponse> {
    return this.http.post<MemoryRestoreResponse>(
      this.url(projectId, '/memory/history/restore'),
      { slug, version },
      this.options(new HttpParams().set('scope', scope)),
    );
  }

  private spaceUrl(spaceId: string, suffix: string): string {
    return `${this.config.appApiUrl()}/memory/spaces/${encodeURIComponent(spaceId)}${suffix}`;
  }

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

/** A memory file name in a path: each `/`-separated part encoded, the separators kept (the route takes `{slug:path}`). */
function encodeSlug(slug: string): string {
  return slug.split('/').map(encodeURIComponent).join('/');
}
