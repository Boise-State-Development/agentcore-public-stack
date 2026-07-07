import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpContext, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import {
  CreateSyncPolicyRequest,
  SyncPoliciesListResponse,
  SyncPolicy,
  UpdateSyncPolicyRequest,
} from '../models/sync-policy.model';

/**
 * Error raised by {@link SyncPolicyService}. `code` is `HTTP_{status}` for
 * server responses (e.g. `HTTP_429` for the run-now cooldown) or `UNKNOWN`.
 * `status` lets callers branch on the contract's meaningful conflicts:
 * 409 (duplicate / reauth-resume / run-now-inactive), 429 (cooldown).
 */
export class SyncPolicyError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'SyncPolicyError';
  }
}

/**
 * Client for the sync-policy endpoints (app-api). All endpoints are
 * edit-gated server-side (owner or editor share) — the knowledge editor
 * only renders sync controls for those roles, so a 403 here means the
 * permission changed underneath the open page.
 */
@Injectable({ providedIn: 'root' })
export class SyncPolicyService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => this.config.appApiUrl());

  /**
   * The knowledge editor surfaces sync errors inline (status lines and
   * toasts with the server's wording), so opt out of the global error toast.
   */
  private requestOptions(): { context: HttpContext } {
    return {
      context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true),
    };
  }

  private policiesUrl(assistantId: string): string {
    return `${this.baseUrl()}/assistants/${encodeURIComponent(assistantId)}/sync-policies`;
  }

  /** List all sync policies for an assistant. */
  async listPolicies(assistantId: string): Promise<SyncPolicy[]> {
    try {
      const response = await firstValueFrom(
        this.http.get<SyncPoliciesListResponse>(
          this.policiesUrl(assistantId),
          this.requestOptions(),
        ),
      );
      return response.policies;
    } catch (err) {
      throw this.toError(err, 'Failed to load sync settings');
    }
  }

  /** Create a policy covering a content source. 409 = source already synced. */
  async createPolicy(
    assistantId: string,
    request: CreateSyncPolicyRequest,
  ): Promise<SyncPolicy> {
    try {
      return await firstValueFrom(
        this.http.post<SyncPolicy>(
          this.policiesUrl(assistantId),
          request,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to enable sync');
    }
  }

  /**
   * Change interval and/or pause/resume. Resuming makes the policy due
   * immediately; resuming a paused_reauth policy is rejected with 409.
   */
  async updatePolicy(
    assistantId: string,
    policyId: string,
    request: UpdateSyncPolicyRequest,
  ): Promise<SyncPolicy> {
    try {
      return await firstValueFrom(
        this.http.patch<SyncPolicy>(
          `${this.policiesUrl(assistantId)}/${encodeURIComponent(policyId)}`,
          request,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to update sync settings');
    }
  }

  /** Delete a policy — the source goes back to manual-only. */
  async deletePolicy(assistantId: string, policyId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.delete<void>(
          `${this.policiesUrl(assistantId)}/${encodeURIComponent(policyId)}`,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to disable sync');
    }
  }

  /**
   * Request an immediate sync (202). The run still flows through the normal
   * dispatcher sweep, so it starts within ~15 minutes. 429 = within the
   * 10-minute cooldown; 409 = policy not active.
   */
  async runNow(assistantId: string, policyId: string): Promise<SyncPolicy> {
    try {
      return await firstValueFrom(
        this.http.post<SyncPolicy>(
          `${this.policiesUrl(assistantId)}/${encodeURIComponent(policyId)}/run-now`,
          null,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to request a sync');
    }
  }

  private toError(err: unknown, fallback: string): SyncPolicyError {
    if (err instanceof HttpErrorResponse) {
      const detail = (err.error as { detail?: string; message?: string } | null) ?? null;
      const message = detail?.detail || detail?.message || err.message || fallback;
      return new SyncPolicyError(message, `HTTP_${err.status}`, err.status);
    }
    if (err instanceof Error) {
      return new SyncPolicyError(err.message || fallback, 'UNKNOWN');
    }
    return new SyncPolicyError(fallback, 'UNKNOWN');
  }
}
