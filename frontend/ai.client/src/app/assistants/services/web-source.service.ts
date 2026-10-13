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
import { KnowledgeTarget, knowledgePaths } from '../models/knowledge-target.model';

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

  /** Kick off a crawl for an agent or a project. Returns the root document + job. */
  async startCrawl(
    target: KnowledgeTarget,
    request: StartCrawlRequest,
  ): Promise<StartCrawlResponse> {
    try {
      return await firstValueFrom(
        this.http.post<StartCrawlResponse>(
          `${this.baseUrl()}${knowledgePaths(target).webSources}/crawl`,
          request,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to start crawl');
    }
  }

  /** List crawls currently `running` for an agent or a project. Empty list when idle. */
  async listActiveCrawls(target: KnowledgeTarget): Promise<CrawlJob[]> {
    const params = new HttpParams().set('active', 'true');
    try {
      const response = await firstValueFrom(
        this.http.get<ActiveCrawlsResponse>(
          `${this.baseUrl()}${knowledgePaths(target).webSources}/crawls`,
          { ...this.requestOptions(), params },
        ),
      );
      return response.crawls;
    } catch (err) {
      throw this.toError(err, 'Failed to load active crawls');
    }
  }

  /**
   * List every crawl for an assistant regardless of status. Completed
   * crawls are the syncable web sources — a web_crawl sync policy's
   * source is the terminal CrawlJob row.
   */
  async listCrawls(target: KnowledgeTarget): Promise<CrawlJob[]> {
    try {
      const response = await firstValueFrom(
        this.http.get<ActiveCrawlsResponse>(
          `${this.baseUrl()}${knowledgePaths(target).webSources}/crawls`,
          this.requestOptions(),
        ),
      );
      return response.crawls;
    } catch (err) {
      throw this.toError(err, 'Failed to load web sources');
    }
  }

  /**
   * Remove a web source — its crawl record, every page it ingested, and any
   * sync policy covering it. Rejects with `HTTP_409` while the crawl is still
   * running (its pages are still being written).
   */
  async deleteCrawl(target: KnowledgeTarget, crawlId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.delete<void>(
          `${this.baseUrl()}${knowledgePaths(target).webSources}/crawls/${encodeURIComponent(crawlId)}`,
          this.requestOptions(),
        ),
      );
    } catch (err) {
      throw this.toError(err, 'Failed to remove web source');
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
