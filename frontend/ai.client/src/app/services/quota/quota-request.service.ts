import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpContext, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../config.service';
import { QuotaStatusService } from './quota-status.service';
import { QuotaWarningService } from './quota-warning.service';
import {
  FundingSource,
  QuotaRequest,
  QuotaRequestCreate,
  QuotaRequestPolicy,
} from './quota-request.model';

/** A refused submit or change, worded for the user. `openRequestId` rides a 409. */
export class QuotaRequestFailure extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly openRequestId?: string,
  ) {
    super(message);
  }
}

/** Notification kinds that change the caller's quota or request state. */
export const QUOTA_NOTIFICATION_KINDS: ReadonlySet<string> = new Set([
  'quota_request_decided',
  'quota_request_info_requested',
  'quota_extension_granted',
  'quota_extension_revoked',
]);

/**
 * The caller's own quota requests (`/quota/*`, quota-requests spec §10.1).
 *
 * Every write reloads `QuotaStatusService`, whose `openRequest` drives what each
 * entry point shows, and this service's own list. Calls opt out of the global
 * error toast: the dialog and the Usage page say what went wrong where the user
 * is looking.
 *
 * Only reached while `features.quotaRequests` is on; with the backend flag off
 * every route 404s.
 */
@Injectable({ providedIn: 'root' })
export class QuotaRequestService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private quotaStatus = inject(QuotaStatusService);
  private quotaWarnings = inject(QuotaWarningService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/quota`);
  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  private readonly requestsState = signal<QuotaRequest[] | null>(null);
  private readonly requestsError = signal<string | null>(null);
  private policyPromise: Promise<QuotaRequestPolicy> | null = null;

  /** The caller's requests, newest first; null until the first load answers. */
  readonly requests = this.requestsState.asReadonly();
  readonly requestsLoadError = this.requestsError.asReadonly();

  /** The dialog's policy. Cached for the tab; a failed read is retried on the next open. */
  policy(): Promise<QuotaRequestPolicy> {
    if (!this.policyPromise) {
      this.policyPromise = firstValueFrom(
        this.http.get<QuotaRequestPolicy>(`${this.baseUrl()}/policy`, this.options),
      ).catch(error => {
        this.policyPromise = null;
        throw error;
      });
    }
    return this.policyPromise;
  }

  async loadRequests(): Promise<void> {
    try {
      const list = await firstValueFrom(
        this.http.get<QuotaRequest[]>(`${this.baseUrl()}/requests`, this.options),
      );
      this.requestsState.set(list);
      this.requestsError.set(null);
    } catch {
      this.requestsError.set('Your requests couldn’t be loaded.');
    }
  }

  async submit(body: QuotaRequestCreate): Promise<QuotaRequest> {
    try {
      return await firstValueFrom(
        this.http.post<QuotaRequest>(`${this.baseUrl()}/requests`, body, this.options),
      );
    } catch (error) {
      throw toFailure(error);
    } finally {
      this.refresh();
    }
  }

  withdraw(requestId: string): Promise<QuotaRequest> {
    return this.patch(requestId, { withdraw: true });
  }

  /** Answer an admin's request for a funding source; the request returns to pending. */
  addFundingSource(requestId: string, fundingSource: FundingSource): Promise<QuotaRequest> {
    return this.patch(requestId, { fundingSource });
  }

  /**
   * A notification about the caller's quota arrived: re-read the status (which
   * is always fresh) and the list. An approval or a grant may have lifted a
   * block, so the composer's blocked tab goes until the next turn says otherwise.
   */
  onQuotaNotification(kind: string): void {
    if (kind === 'quota_request_decided' || kind === 'quota_extension_granted') {
      this.quotaWarnings.clearQuotaExceeded();
    }
    this.refresh();
  }

  /** Re-read the status and, once loaded, the list. */
  refresh(): void {
    this.quotaStatus.reload();
    if (this.requestsState() !== null) void this.loadRequests();
  }

  private async patch(requestId: string, body: object): Promise<QuotaRequest> {
    try {
      return await firstValueFrom(
        this.http.patch<QuotaRequest>(
          `${this.baseUrl()}/requests/${encodeURIComponent(requestId)}`,
          body,
          this.options,
        ),
      );
    } catch (error) {
      throw toFailure(error);
    } finally {
      this.refresh();
    }
  }
}

/** The backend's `{detail}` (a string from the service, a list from validation) as one sentence. */
function toFailure(error: unknown): QuotaRequestFailure {
  if (!(error instanceof HttpErrorResponse)) {
    return new QuotaRequestFailure('Something went wrong. Try again.', 0);
  }
  const body = error.error as { detail?: unknown; openRequestId?: string } | null;
  const detail = body?.detail;
  let message = 'Something went wrong. Try again.';
  if (typeof detail === 'string' && detail) {
    message = detail;
  } else if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === 'string') message = first.msg.replace(/^Value error, /, '');
  } else if (error.status === 404) {
    message = 'Quota requests aren’t available right now.';
  }
  return new QuotaRequestFailure(message, error.status, body?.openRequestId);
}
