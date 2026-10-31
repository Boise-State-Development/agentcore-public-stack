import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpContext, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import {
  AdminQuotaRequest,
  AdminQuotaRequestPolicy,
  ApproveRequestBody,
  ApproveResponse,
  ExtensionCreateBody,
  QuotaExtension,
  QuotaRequestDetail,
  QuotaRequestListResponse,
  QuotaRequestStatus,
  QuotaRequestSummary,
} from '../models/quota-requests-admin.models';

/**
 * `/admin/quota-requests` and `/admin/quota-extensions` (quota-requests spec §10.2).
 *
 * Calls opt out of the global error toast: every page and dialog here says what
 * went wrong where the admin is looking (a refused self-decision, a request
 * another admin just decided), and a toast on top would say it twice.
 *
 * Every route sits behind `admin.quota_requests` and 404s while the backend's
 * `QUOTA_REQUESTS_ENABLED` is off; the pages are only reachable with
 * `features.quotaRequests` on.
 */
@Injectable({ providedIn: 'root' })
export class QuotaRequestsAdminService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly requestsUrl = computed(() => `${this.config.appApiUrl()}/admin/quota-requests`);
  private readonly extensionsUrl = computed(() => `${this.config.appApiUrl()}/admin/quota-extensions`);
  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  private readonly summaryState = signal<QuotaRequestSummary>({ pending: 0, infoRequested: 0 });

  /** Requests waiting on an admin: the nav and tab badge. */
  readonly pendingCount = computed(() => this.summaryState().pending);

  /** Re-reads the badge counts. A badge is orientation, so a failed read keeps the last answer. */
  async refreshSummary(): Promise<void> {
    try {
      this.summaryState.set(
        await firstValueFrom(this.http.get<QuotaRequestSummary>(`${this.requestsUrl()}/summary`, this.options)),
      );
    } catch {
      // Leave whatever the last successful read said.
    }
  }

  async listRequests(status: QuotaRequestStatus, cursor?: string | null): Promise<QuotaRequestListResponse> {
    let params = new HttpParams().set('status', status).set('limit', '100');
    if (cursor) params = params.set('cursor', cursor);
    return firstValueFrom(
      this.http.get<QuotaRequestListResponse>(this.requestsUrl(), { ...this.options, params }),
    );
  }

  async getRequest(requestId: string): Promise<QuotaRequestDetail> {
    return firstValueFrom(
      this.http.get<QuotaRequestDetail>(`${this.requestsUrl()}/${encodeURIComponent(requestId)}`, this.options),
    );
  }

  async approve(requestId: string, body: ApproveRequestBody): Promise<ApproveResponse> {
    const result = await firstValueFrom(
      this.http.post<ApproveResponse>(`${this.requestsUrl()}/${encodeURIComponent(requestId)}/approve`, body, this.options),
    );
    void this.refreshSummary();
    return result;
  }

  async requestInfo(requestId: string, note: string): Promise<AdminQuotaRequest> {
    const result = await firstValueFrom(
      this.http.post<AdminQuotaRequest>(
        `${this.requestsUrl()}/${encodeURIComponent(requestId)}/request-info`,
        { note },
        this.options,
      ),
    );
    void this.refreshSummary();
    return result;
  }

  async deny(requestId: string, note: string): Promise<AdminQuotaRequest> {
    const result = await firstValueFrom(
      this.http.post<AdminQuotaRequest>(`${this.requestsUrl()}/${encodeURIComponent(requestId)}/deny`, { note }, this.options),
    );
    void this.refreshSummary();
    return result;
  }

  async listExtensions(options: { userId?: string; active?: boolean } = {}): Promise<QuotaExtension[]> {
    let params = new HttpParams();
    if (options.userId) params = params.set('userId', options.userId);
    if (options.active) params = params.set('active', 'true');
    return firstValueFrom(this.http.get<QuotaExtension[]>(this.extensionsUrl(), { ...this.options, params }));
  }

  async createExtension(body: ExtensionCreateBody): Promise<QuotaExtension> {
    return firstValueFrom(this.http.post<QuotaExtension>(this.extensionsUrl(), body, this.options));
  }

  async revokeExtension(extensionId: string, reason: string): Promise<QuotaExtension> {
    return firstValueFrom(
      this.http.post<QuotaExtension>(
        `${this.extensionsUrl()}/${encodeURIComponent(extensionId)}/revoke`,
        { reason },
        this.options,
      ),
    );
  }

  async getPolicy(): Promise<AdminQuotaRequestPolicy> {
    return firstValueFrom(this.http.get<AdminQuotaRequestPolicy>(`${this.requestsUrl()}/policy`, this.options));
  }

  async updatePolicy(policy: AdminQuotaRequestPolicy): Promise<AdminQuotaRequestPolicy> {
    return firstValueFrom(this.http.put<AdminQuotaRequestPolicy>(`${this.requestsUrl()}/policy`, policy, this.options));
  }
}

/**
 * One sentence an admin can act on, from whatever the API refused with.
 *
 * The quota routes answer `{detail: "…"}`; a body that failed validation answers
 * FastAPI's list of errors, which reads as noise, so it collapses to the first message.
 */
export function quotaAdminErrorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof HttpErrorResponse)) return fallback;
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === 'string') return first.msg.replace(/^Value error, /, '');
  }
  if (error.status === 403) return 'You do not have permission to do that.';
  if (error.status === 404) return 'Not found. It may have been removed, or quota requests are switched off.';
  return fallback;
}
