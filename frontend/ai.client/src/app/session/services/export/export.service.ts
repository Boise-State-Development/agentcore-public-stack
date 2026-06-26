import { Injectable, inject, computed } from '@angular/core';
import {
  HttpClient,
  HttpContext,
  HttpErrorResponse,
} from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import {
  ExportRequest,
  ExportResponse,
  ExportTargetConnector,
  ExportTargetListResponse,
} from './export.model';

/**
 * Error raised by {@link ExportService} for any failed call. `code` is
 * `HTTP_{status}` for server responses (e.g. `HTTP_409` when the connector
 * needs consent, `HTTP_404` when it isn't an export target) or `UNKNOWN`.
 * Mirrors `FileSourceError`.
 */
export class ExportError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'ExportError';
  }
}

/**
 * Client for the user-facing export-target endpoints (app-api).
 *
 * Lets the chat surface discover which connectors can receive a conversation
 * and save a transcript out to one. All routes live on app-api — the
 * AgentCore runtime data plane only proxies `/invocations` and `/ping`. The
 * write-direction mirror of {@link FileSourceService}.
 */
@Injectable({ providedIn: 'root' })
export class ExportService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => this.config.appApiUrl());

  /**
   * Header app-api's `AgentCoreContextMiddleware` bridges into
   * `BedrockAgentCoreContext` so the identity client has a callback URL for
   * AgentCore Identity. Every export endpoint resolves an OAuth token
   * server-side, so this must accompany every call — without it the backend
   * raises `CallbackUrlUnavailableError` (503). Mirrors `FileSourceService`.
   *
   * The backend re-appends `provider_id` itself, and the middleware rejects
   * URLs carrying a query string as a redirect-pivot guard — so we send a
   * bare `/oauth-complete`.
   */
  private callbackHeaders(): Record<string, string> {
    const callback = new URL('/oauth-complete', window.location.origin);
    return { OAuth2CallbackUrl: callback.toString() };
  }

  /**
   * Per-request options shared by every export call. `SUPPRESS_ERROR_TOAST`
   * opts these out of the global error toast — the export dialog renders its
   * own inline, actionable errors (e.g. a Connect button on a 409), so the
   * generic toast would only be a confusing duplicate.
   */
  private requestOptions(): {
    headers: Record<string, string>;
    context: HttpContext;
  } {
    return {
      headers: this.callbackHeaders(),
      context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true),
    };
  }

  /** List the connectors the current user can save a conversation to. */
  async listExportTargets(): Promise<ExportTargetConnector[]> {
    try {
      const response = await firstValueFrom(
        this.http.get<ExportTargetListResponse>(
          `${this.baseUrl()}/export-targets`,
          this.requestOptions(),
        ),
      );
      return response.exportTargets;
    } catch (err) {
      throw this.toError(err, 'Failed to load export targets');
    }
  }

  /** Save a conversation transcript to a connected app. */
  async exportSession(
    sessionId: string,
    request: ExportRequest,
  ): Promise<ExportResponse> {
    try {
      return await firstValueFrom(
        this.http.post<ExportResponse>(
          `${this.baseUrl()}/sessions/${encodeURIComponent(sessionId)}/export`,
          request,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to save the conversation');
    }
  }

  private toError(err: unknown, fallback: string): ExportError {
    if (err instanceof HttpErrorResponse) {
      const detail = (err.error as { detail?: string; message?: string } | null) ?? null;
      const message = detail?.detail || detail?.message || err.message || fallback;
      return new ExportError(message, `HTTP_${err.status}`, err.status);
    }
    if (err instanceof Error) {
      return new ExportError(err.message || fallback, 'UNKNOWN');
    }
    return new ExportError(fallback, 'UNKNOWN');
  }
}
