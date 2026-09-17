import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  PREVIEW_KIND_MIMES,
  PreviewKind,
  previewFetchesBytes,
  previewKindFor,
} from './file-preview.model';
import { SheetPreviewResponse } from './sheet-preview.model';

/** `GET /files/{uploadId}/preview-url` — camelCase aliases on the wire. */
interface PreviewUrlResponseDto {
  uploadId: string;
  url: string;
  expiresAt: string;
  mimeType: string;
  filename: string;
}

/** A fetched document, ready to hand to the renderer. */
export interface PreviewDocument {
  bytes: ArrayBuffer;
  mimeType: string;
  filename: string;
  /** Which viewer should render these bytes. */
  kind: PreviewKind;
}

/** The preview failed in a way the pane should explain, not swallow. */
export class FilePreviewError extends Error {
  constructor(
    message: string,
    /** True when retrying could plausibly work (network, expiry) — a
     *  wrong MIME type or a deleted file will fail the same way twice. */
    readonly retryable: boolean,
  ) {
    super(message);
    this.name = 'FilePreviewError';
  }
}

/**
 * Fetches the bytes of an uploaded file for in-browser preview.
 *
 * Two legs, and they must stay two:
 *
 * 1. `GET /files/{id}/preview-url` on app-api, cookie-authenticated.
 *    The route is owner-scoped and READY-gated, so authorization is
 *    settled server-side before any bytes move.
 * 2. A plain `fetch` of the presigned S3 URL it returns.
 *
 * Leg 2 is deliberately NOT `HttpClient`. The app's interceptor chain
 * would attach the CSRF header and route S3's own 403s (an expired
 * signature, most likely) into the global error interceptor, which
 * treats an auth failure as a reason to bounce the user to login. A
 * presigned URL going stale is a retry, not a logout.
 *
 * `credentials: 'omit'` is load-bearing for the same leg: S3 answers a
 * CORS GET with `Access-Control-Allow-Origin` but no
 * `Access-Control-Allow-Credentials`, so a credentialed request is
 * rejected by the browser before it is even sent. Nothing is lost — the
 * signature in the URL *is* the authorization.
 *
 * `/preview-url` rather than `/download`: it signs
 * `Content-Disposition: inline` and, more usefully here, returns the
 * server's recorded `mimeType`, which is what lets us refuse a
 * mislabelled file before handing it to the renderer.
 */
@Injectable({ providedIn: 'root' })
export class FilePreviewHttpService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  /**
   * Resolve `uploadId` to a previewable document's bytes.
   *
   * Rejects with a `FilePreviewError` on every failure path so the pane
   * has one thing to catch and a `retryable` flag to decide whether to
   * offer the button.
   *
   * The MIME type the server recorded must agree with the extension the
   * card was rendered from. Checking both directions is the point: the
   * extension chose the viewer before any request was made, so a file
   * named `.pptx` that the server knows to be a `.docx` has to fail here
   * rather than reach a renderer that cannot read it.
   */
  async fetchDocument(uploadId: string): Promise<PreviewDocument> {
    const meta = await this.requestPreviewUrl(uploadId);

    const kind = previewKindFor(meta.filename);
    if (kind === null || !PREVIEW_KIND_MIMES[kind].includes(meta.mimeType)) {
      throw new FilePreviewError(
        `This file is a ${meta.mimeType || 'unknown type'}, which can't be previewed here.`,
        false,
      );
    }

    // A kind that is read server-side must never reach the byte path.
    // Nothing here could render the result, and the cost of finding out
    // is a whole workbook pulled into the browser and thrown away — so
    // this fails loudly rather than wasting the transfer. `fetchSheets`
    // is the route for those.
    if (!previewFetchesBytes(kind)) {
      throw new FilePreviewError(
        'This file is read on the server; use fetchSheets instead.',
        false,
      );
    }

    const bytes = await this.fetchBytes(meta.url);
    return { bytes, mimeType: meta.mimeType, filename: meta.filename, kind };
  }

  private async requestPreviewUrl(
    uploadId: string,
  ): Promise<PreviewUrlResponseDto> {
    try {
      return await firstValueFrom(
        this.http.get<PreviewUrlResponseDto>(
          `${this.config.appApiUrl()}/files/${encodeURIComponent(uploadId)}/preview-url`,
        ),
      );
    } catch {
      // A 404 here means deleted, not-owned, or still processing — all
      // of which read the same to the user and none of which a retry
      // fixes within the life of this pane. Everything else (a 5xx, a
      // dropped connection) is worth another go, and offering the
      // button on the ambiguous case is the kinder default.
      throw new FilePreviewError(
        'This document is no longer available.',
        true,
      );
    }
  }

  /**
   * Fetch an `.xlsx` as rows, rather than as bytes.
   *
   * The only preview that does not go through `fetchDocument`, and the
   * reason is the renderer landscape rather than anything about the
   * file: there is no client-side workbook reader we are willing to
   * ship, so app-api reads it with openpyxl and sends values. No
   * presigned URL and no second leg — the workbook never reaches the
   * browser at all.
   */
  async fetchSheets(uploadId: string): Promise<SheetPreviewResponse> {
    try {
      return await firstValueFrom(
        this.http.get<SheetPreviewResponse>(
          `${this.config.appApiUrl()}/files/${encodeURIComponent(uploadId)}/sheet-preview`,
        ),
      );
    } catch (e) {
      throw sheetPreviewError(e);
    }
  }

  private async fetchBytes(url: string): Promise<ArrayBuffer> {
    let response: Response;
    try {
      response = await fetch(url, { credentials: 'omit' });
    } catch {
      throw new FilePreviewError(
        "Couldn't reach the document. Check your connection and try again.",
        true,
      );
    }

    if (!response.ok) {
      throw new FilePreviewError(
        `Couldn't download the document (${response.status}).`,
        true,
      );
    }

    return response.arrayBuffer();
  }
}

/**
 * Turn a `/sheet-preview` failure into something the pane can show.
 *
 * The route distinguishes its failures on purpose, and each one means a
 * different thing to the user: 413 and 422 are permanent facts about the
 * file that no retry changes, while a 5xx or a dropped connection is
 * worth another go. 415 should be unreachable — the extension chose this
 * viewer — so it reads as the mislabelled file it is.
 */
function sheetPreviewError(e: unknown): FilePreviewError {
  const status = (e as { status?: number })?.status ?? 0;
  switch (status) {
    case 404:
      return new FilePreviewError('This workbook is no longer available.', true);
    case 413:
      return new FilePreviewError(
        'This workbook is too large to preview. Download it to open in Excel.',
        false,
      );
    case 415:
      return new FilePreviewError(
        "This file isn't a spreadsheet the preview can read.",
        false,
      );
    case 422:
      return new FilePreviewError(
        'This workbook could not be read. It may be corrupt or password-protected.',
        false,
      );
    default:
      return new FilePreviewError(
        "Couldn't load this workbook. Check your connection and try again.",
        true,
      );
  }
}
