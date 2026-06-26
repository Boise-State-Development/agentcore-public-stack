/**
 * Models for saving a conversation out to a connected app ("export targets").
 *
 * The write-direction mirror of the file-source models: where a file source
 * reads documents *into* an assistant, an export target writes a conversation
 * transcript *out* to a connected app (e.g. Google Drive). Shapes match the
 * app-api `export_targets` endpoints exactly.
 */

/** Output format an export produces. Matches the backend `ExportFormat` enum. */
export type ExportFormat = 'google_doc' | 'markdown' | 'pdf';

/**
 * A connector the current user can save a conversation to, as returned by
 * `GET /export-targets`. The write-direction analog of `FileSourceConnector`.
 */
export interface ExportTargetConnector {
  providerId: string;
  displayName: string;
  iconName: string;
  /** Optional admin-uploaded icon (base64 data URL). Wins over `iconName`. */
  iconData?: string | null;
  /** True when the vault holds a usable token — the user can save straight away. */
  connected: boolean;
  /** Output formats this destination accepts, driving the format picker. */
  supportedFormats: ExportFormat[];
  /**
   * True when this connector is also a file source, so the SPA can reuse the
   * import browse dialog to pick a destination folder. False means exports land
   * in the adapter's default app folder and the folder picker is hidden.
   */
  browsable: boolean;
}

export interface ExportTargetListResponse {
  exportTargets: ExportTargetConnector[];
}

/**
 * Which conversation elements to include in the exported transcript. Sent as
 * the `include` object on the export request; omitted fields fall back to the
 * backend defaults. The two message flags are always on (the transcript
 * itself) and rendered as disabled-checked in the dialog.
 */
export interface ExportInclude {
  userMessages: boolean;
  assistantMessages: boolean;
  toolCalls: boolean;
  images: boolean;
  citations: boolean;
  reasoning: boolean;
  timestamps: boolean;
}

/** Default include selection — matches the backend `ExportInclude` defaults. */
export const DEFAULT_EXPORT_INCLUDE: ExportInclude = {
  userMessages: true,
  assistantMessages: true,
  toolCalls: true,
  images: true,
  citations: true,
  reasoning: false,
  timestamps: false,
};

/** Body for `POST /sessions/{id}/export`. */
export interface ExportRequest {
  connectorId: string;
  format?: ExportFormat;
  parentId?: string;
  include?: ExportInclude;
}

/**
 * Receipt for a successful export, persisted on the session so a
 * "Saved · Open" affordance survives a reload. Matches the backend
 * `ExportReceipt`.
 */
export interface ExportReceipt {
  connectorId: string;
  adapterKey: string;
  format: string;
  fileId: string;
  fileName: string;
  webViewLink?: string | null;
  exportedAt: string;
}

/** Result of `POST /sessions/{id}/export`. */
export interface ExportResponse {
  fileId: string;
  name: string;
  webViewLink?: string | null;
  receipt: ExportReceipt;
}
