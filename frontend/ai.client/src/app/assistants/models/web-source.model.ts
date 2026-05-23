/**
 * Front-end models for the web-source ingestion feature.
 *
 * Mirrors `apis/app_api/web_sources/models.py` — field names are the
 * camelCase aliases the backend serializes. A "web source" is just a URL
 * the user wants to ingest; with `maxDepth=0` it's a single-page import,
 * higher values trigger a BFS crawl.
 */

import { Document } from './document.model';

/** Tunable bounds for a single crawl job. Defaults mirror the backend. */
export interface CrawlSettings {
  maxDepth: number;
  maxPages: number;
  concurrency: number;
  minDelay: number;
  maxDelay: number;
  sameDomainOnly: boolean;
}

/** The polite defaults the SPA seeds the modal with. Match backend `CrawlSettings()`. */
export const DEFAULT_CRAWL_SETTINGS: CrawlSettings = {
  maxDepth: 2,
  maxPages: 25,
  concurrency: 2,
  minDelay: 1,
  maxDelay: 3,
  sameDomainOnly: true,
};

/** Single-page mode: the BFS visits only the root URL. */
export const SINGLE_PAGE_SETTINGS: CrawlSettings = {
  ...DEFAULT_CRAWL_SETTINGS,
  maxDepth: 0,
  maxPages: 1,
  concurrency: 1,
};

export type CrawlJobStatus = 'running' | 'complete' | 'failed';

/** A web-crawl job persisted alongside the assistant. */
export interface CrawlJob {
  crawlId: string;
  assistantId: string;
  rootUrl: string;
  status: CrawlJobStatus;
  settings: CrawlSettings;
  discoveredCount: number;
  fetchedCount: number;
  failedCount: number;
  startedAt: string;
  completedAt?: string | null;
  startedByUserId: string;
  error?: string | null;
}

/** Request body for `POST /assistants/{id}/web-sources/crawl`. */
export interface StartCrawlRequest {
  url: string;
  settings?: CrawlSettings;
}

/**
 * Response from `POST /assistants/{id}/web-sources/crawl`. Includes the
 * pre-created root `Document` so the SPA can immediately render and poll it.
 */
export interface StartCrawlResponse {
  crawl: CrawlJob;
  documents: Document[];
}

/** Response from `GET /assistants/{id}/web-sources/crawls?active=true`. */
export interface ActiveCrawlsResponse {
  crawls: CrawlJob[];
}
