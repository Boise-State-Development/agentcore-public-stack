import { Injectable, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../services/config.service';

/**
 * The derived, UI-facing phase of a knowledge base upgrade.
 *
 * Deliberately not the backend record's internal migration state: `shadow`,
 * `verify` and `promote` all arrive here as `in_progress`, so the client cannot
 * grow a dependency on step names that belong to the worker.
 *
 * `none` means render nothing at all — no badge, no banner, no prompt.
 */
export type UpgradePhase = 'none' | 'available' | 'in_progress' | 'succeeded' | 'failed';

/**
 * Why a document will not be carried across.
 *
 * `unsupported_format` and `processing_failure` are separate because the user's
 * next action differs: convert and re-upload, versus retry. Telling someone to
 * retry a file the platform cannot read teaches them the retry button is broken.
 */
export type DocumentIssueKind =
  | 'unsupported_format'
  | 'processing_failure'
  | 'still_processing'
  | 'being_removed';

export interface UpgradeProgress {
  completed: number;
  total: number;
  skipped: number;
}

export interface DocumentNotCarried {
  documentId: string;
  filename: string;
  /** The stored processing status, verbatim. Not for display. */
  status: string;
  kind: DocumentIssueKind;
  /** Plain-language explanation from the server, safe to render directly. */
  message: string;
  retryable: boolean;
}

export interface UpgradeStatus {
  phase: UpgradePhase;
  canUpgrade: boolean;
  progress: UpgradeProgress | null;
  reason: string | null;
  noticePending: boolean;
  documentsNotCarried: DocumentNotCarried[];
}

export interface UpgradeResult {
  phase: UpgradePhase;
  /** False when the call found an upgrade already running. Still a success. */
  started: boolean;
  message: string;
}

/** What the client falls back to when the status call fails. */
const NOTHING_TO_SHOW: UpgradeStatus = {
  phase: 'none',
  canUpgrade: false,
  progress: null,
  reason: null,
  noticePending: false,
  documentsNotCarried: [],
};

/**
 * The knowledge base upgrade surface.
 *
 * Every method fails soft. This is an optional card on a page whose primary job
 * — uploading and listing documents — works regardless, so a failing upgrade
 * endpoint must never be able to take the section down with it.
 */
@Injectable({ providedIn: 'root' })
export class KbUpgradeService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/assistants`);

  private url(entityId: string, suffix = ''): string {
    return `${this.baseUrl()}/${entityId}/knowledge-base/upgrade${suffix}`;
  }

  /**
   * Read what the card should render.
   *
   * Resolves to `phase: 'none'` rather than rejecting, so a caller can assign
   * the result straight to a signal. A viewer gets an honest phase but never
   * `canUpgrade`; the server decides that, not this client.
   */
  async getStatus(entityId: string): Promise<UpgradeStatus> {
    try {
      const status = await firstValueFrom(
        this.http.get<UpgradeStatus>(this.url(entityId)),
      );
      return { ...NOTHING_TO_SHOW, ...status };
    } catch {
      return NOTHING_TO_SHOW;
    }
  }

  /** Opt in. Rejects with a user-safe message so the caller can toast it. */
  async start(entityId: string): Promise<UpgradeResult> {
    return this.post(entityId, '');
  }

  /** Restart after a failure, on a fresh generation server-side. */
  async retry(entityId: string): Promise<UpgradeResult> {
    return this.post(entityId, '/retry');
  }

  /**
   * Dismiss the one-time success notice.
   *
   * Swallows failure: the notice is already hidden locally by the time this is
   * called, and an error toast for "we could not forget something" is noise.
   * The worst case is the notice returning on the next page load.
   */
  async dismissNotice(entityId: string): Promise<void> {
    try {
      await firstValueFrom(this.http.post<void>(this.url(entityId, '/notice'), {}));
    } catch {
      // Intentionally ignored — see above.
    }
  }

  private async post(entityId: string, suffix: string): Promise<UpgradeResult> {
    try {
      return await firstValueFrom(
        this.http.post<UpgradeResult>(this.url(entityId, suffix), {}),
      );
    } catch (err: unknown) {
      throw new Error(this.messageFrom(err));
    }
  }

  /**
   * Prefer the server's `detail`, which is written for a user.
   *
   * The generic fallback never mentions a status code: "409" tells the reader
   * nothing they can act on.
   */
  private messageFrom(err: unknown): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
    return 'The upgrade could not be started. Nothing has changed — please try again.';
  }
}
