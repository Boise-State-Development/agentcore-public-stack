import { Injectable, computed, inject } from '@angular/core';
import {
  HttpClient,
  HttpContext,
  HttpErrorResponse,
  HttpParams,
} from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import {
  ActiveCrawlsResponse,
  CrawlJob,
  StartCrawlRequest,
  StartCrawlResponse,
} from '../models/web-source.model';

/**
 * Error raised by {@link WebSourceService}. `code` is `HTTP_{status}` for
 * server responses (e.g. `HTTP_422` for an invalid URL) or `UNKNOWN`.
 */
export class WebSourceError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'WebSourceError';
  }
}

/**
 * Client for the user-facing web-source endpoints (app-api).
 *
 * The editor uses this to start a crawl from a URL and to poll for active
 * crawls so it can refresh its document list while pages stream in.
 */
@Injectable({ providedIn: 'root' })
export class WebSourceService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => this.config.appApiUrl());

  /**
   * The dialog and the editor banner render their own inline errors, so opt
   * out of the global error toast — a duplicate banner would only confuse.
   */
  private requestOptions(): { context: HttpContext } {
    return {
      context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true),
    };
  }

  /** Kick off a crawl for the given assistant. Returns the root document + job. */
  async startCrawl(
    assistantId: string,
    request: StartCrawlRequest,
  ): Promise<StartCrawlResponse> {
    try {
      return await firstValueFrom(
        this.http.post<StartCrawlResponse>(
          `${this.baseUrl()}/assistants/${encodeURIComponent(assistantId)}/web-sources/crawl`,
          request,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to start crawl');
    }
  }

  /** List crawls currently `running` for an assistant. Empty list when idle. */
  async listActiveCrawls(assistantId: string): Promise<CrawlJob[]> {
    const params = new HttpParams().set('active', 'true');
    try {
      const response = await firstValueFrom(
        this.http.get<ActiveCrawlsResponse>(
          `${this.baseUrl()}/assistants/${encodeURIComponent(assistantId)}/web-sources/crawls`,
          { ...this.requestOptions(), params },
        ),
      );
      return response.crawls;
    } catch (err) {
      throw this.toError(err, 'Failed to load active crawls');
    }
  }

  private toError(err: unknown, fallback: string): WebSourceError {
    if (err instanceof HttpErrorResponse) {
      const detail =
        (err.error as { detail?: string; message?: string } | null) ?? null;
      const message = detail?.detail || detail?.message || err.message || fallback;
      return new WebSourceError(message, `HTTP_${err.status}`, err.status);
    }
    if (err instanceof Error) {
      return new WebSourceError(err.message || fallback, 'UNKNOWN');
    }
    return new WebSourceError(fallback, 'UNKNOWN');
  }
}
