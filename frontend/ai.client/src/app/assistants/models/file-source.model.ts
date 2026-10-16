/**
 * Front-end models for the file-source connector feature.
 *
 * A connector becomes a "file source" only when an admin maps it to a
 * file-source adapter. These interfaces mirror the backend response contract
 * from `apis/app_api/file_sources/` and the browse/import endpoints — field
 * names are the camelCase aliases the backend serializes.
 */

import { Document } from './document.model';

/** One connector the current user can use as a file source. */
export interface FileSourceConnector {
  providerId: string;
  displayName: string;
  iconName: string;
  /** Optional admin-uploaded icon (base64 data URL). Wins over `iconName`. */
  iconData?: string | null;
  /** True when the user already has a usable OAuth token for this connector. */
  connected: boolean;
}

/** Response from GET /file-sources. */
export interface FileSourceListResponse {
  fileSources: FileSourceConnector[];
}

/**
 * A top-level browsing root a provider exposes. Providers don't share a
 * single tree — Google Drive has My Drive, Shared with me, and N shared
 * drives as distinct roots.
 */
export interface SourceRoot {
  id: string;
  name: string;
}

/** Response from GET /connectors/{id}/roots. */
export interface SourceRootsResponse {
  roots: SourceRoot[];
}

/** Whether a browse entry is a navigable folder or a selectable file. */
export type FileEntryType = 'folder' | 'file';

/** A single folder or file returned by a browse/search call. */
export interface FileEntry {
  id: string;
  name: string;
  type: FileEntryType;
  mimeType?: string | null;
  sizeBytes?: number | null;
  modifiedAt?: string | null;
  etag?: string | null;
  /**
   * False for folders and for files that cannot be ingested (e.g. Google
   * Forms). The browser renders non-selectable files disabled.
   */
  selectable: boolean;
  /**
   * The MIME type the file's bytes arrive as when an export changes it: a
   * Google Doc downloads as a .docx. Absent when the file downloads as
   * itself. The chat composer judges attachability on this, not `mimeType`.
   */
  downloadMimeType?: string | null;
}

/** One hop in the folder path shown above the browser. */
export interface Breadcrumb {
  id: string;
  name: string;
}

/** A page of folder contents or search results. */
export interface BrowseResult {
  entries: FileEntry[];
  breadcrumbs: Breadcrumb[];
  /** Opaque pagination cursor; null/absent when there are no further pages. */
  nextCursor?: string | null;
}

/** One file selected for import from a connected file source. */
export interface ImportFileRef {
  fileId: string;
  name: string;
}

/**
 * Request body for POST /files/import: attach one file from a connected file
 * source to a conversation. One file per call so each gets its own card,
 * progress and retry.
 */
export interface AttachSourceFileRequest {
  sessionId: string;
  connectorId: string;
  fileId: string;
}

/** Request body for POST /assistants/{id}/documents/import. */
export interface ImportDocumentsRequest {
  connectorId: string;
  files: ImportFileRef[];
}

/**
 * Response from POST /assistants/{id}/documents/import. Each document starts
 * in 'uploading' state and is polled the same way as a device upload.
 */
export interface ImportDocumentsResponse {
  documents: Document[];
}
