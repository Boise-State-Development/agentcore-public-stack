import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  BrowseResult,
  FileSourceConnector,
  FileSourceListResponse,
  ImportDocumentsResponse,
  ImportFileRef,
  SourceRoot,
  SourceRootsResponse,
} from '../models/file-source.model';

/**
 * Error raised by {@link FileSourceService} for any failed call. `code` is
 * `HTTP_{status}` for server responses (e.g. `HTTP_409` when the connector
 * needs consent, `HTTP_404` when it isn't a file source) or `UNKNOWN`.
 */
export class FileSourceError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'FileSourceError';
  }
}

/**
 * Client for the user-facing file-source endpoints (app-api).
 *
 * Lets the assistant editor discover which connectors are usable as file
 * sources, walk the provider's folder tree, and import selected files into
 * an assistant's RAG index. All routes live on app-api — the AgentCore
 * runtime data plane only proxies `/invocations` and `/ping`.
 */
@Injectable({ providedIn: 'root' })
export class FileSourceService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => this.config.appApiUrl());

  /** List the connectors the current user can use as a file source. */
  async listFileSources(): Promise<FileSourceConnector[]> {
    try {
      const response = await firstValueFrom(
        this.http.get<FileSourceListResponse>(`${this.baseUrl()}/file-sources`),
      );
      return response.fileSources;
    } catch (err) {
      throw this.toError(err, 'Failed to load file sources');
    }
  }

  /** List the top-level browsing roots for a connected file source. */
  async listRoots(connectorId: string): Promise<SourceRoot[]> {
    try {
      const response = await firstValueFrom(
        this.http.get<SourceRootsResponse>(
          `${this.baseUrl()}/connectors/${encodeURIComponent(connectorId)}/roots`,
        ),
      );
      return response.roots;
    } catch (err) {
      throw this.toError(err, 'Failed to load folders');
    }
  }

  /** List one page of a folder's contents. */
  async browse(
    connectorId: string,
    folderId: string,
    cursor?: string | null,
  ): Promise<BrowseResult> {
    let params = new HttpParams().set('folder_id', folderId);
    if (cursor) {
      params = params.set('cursor', cursor);
    }
    try {
      return await firstValueFrom(
        this.http.get<BrowseResult>(
          `${this.baseUrl()}/connectors/${encodeURIComponent(connectorId)}/browse`,
          { params },
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to open folder');
    }
  }

  /** Search a file source by free-text query, one page at a time. */
  async search(
    connectorId: string,
    query: string,
    cursor?: string | null,
  ): Promise<BrowseResult> {
    let params = new HttpParams().set('query', query);
    if (cursor) {
      params = params.set('cursor', cursor);
    }
    try {
      return await firstValueFrom(
        this.http.get<BrowseResult>(
          `${this.baseUrl()}/connectors/${encodeURIComponent(connectorId)}/search`,
          { params },
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Search failed');
    }
  }

  /**
   * Import the selected files into an assistant. The backend creates one
   * document per file (status 'uploading') and downloads them asynchronously.
   */
  async importDocuments(
    assistantId: string,
    connectorId: string,
    files: ImportFileRef[],
  ): Promise<ImportDocumentsResponse> {
    try {
      return await firstValueFrom(
        this.http.post<ImportDocumentsResponse>(
          `${this.baseUrl()}/assistants/${encodeURIComponent(assistantId)}/documents/import`,
          { connectorId, files },
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to import files');
    }
  }

  private toError(err: unknown, fallback: string): FileSourceError {
    if (err instanceof HttpErrorResponse) {
      const detail = (err.error as { detail?: string; message?: string } | null) ?? null;
      const message = detail?.detail || detail?.message || err.message || fallback;
      return new FileSourceError(message, `HTTP_${err.status}`, err.status);
    }
    if (err instanceof Error) {
      return new FileSourceError(err.message || fallback, 'UNKNOWN');
    }
    return new FileSourceError(fallback, 'UNKNOWN');
  }
}
