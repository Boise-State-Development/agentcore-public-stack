import { Injectable, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../config.service';

/**
 * What an attachment card shows of a Word, spreadsheet, CSV or deck file,
 * written by the prepare Lambda at upload (`_derived/preview.json`).
 */
export type AttachmentPreview =
  | { version: 1; kind: 'doc'; lines: string[] }
  | { version: 1; kind: 'sheet'; sheets: string[]; rows: string[][] }
  | { version: 1; kind: 'deck'; slides: number; title: string; bullets: string[] };

interface PreviewsResponse {
  previews: Record<string, AttachmentPreview | null>;
}

/** Most ids one request carries: a message's attachment limit. */
const MAX_IDS = 20;
/** How long asks wait to be sent together. */
const BATCH_WINDOW_MS = 25;
/** Waits before asking again for a fresh upload whose preview is not written yet. */
const FRESH_RETRY_DELAYS_MS = [3000, 6000];

/**
 * Card previews, fetched in batches: every card that asks within a few
 * milliseconds of another shares one `GET /files/previews?ids=` call, so a
 * message of twenty files makes one request rather than twenty.
 *
 * A preview is cached for the page's life (it never changes for an upload).
 * `null` is cached too, except for a fresh upload, whose preview the Lambda
 * may still be writing: that one is asked again after 3 and 6 seconds.
 */
@Injectable({ providedIn: 'root' })
export class AttachmentPreviewsService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/files`);

  private readonly cache = new Map<string, Promise<AttachmentPreview | null>>();
  private queue = new Map<string, (preview: AttachmentPreview | null) => void>();
  private timer: ReturnType<typeof setTimeout> | null = null;

  /** The preview of `uploadId`, or `null` when there is none to show. */
  preview(uploadId: string, options: { fresh?: boolean } = {}): Promise<AttachmentPreview | null> {
    const cached = this.cache.get(uploadId);
    if (cached) return cached;
    const pending = options.fresh ? this.askUntilWritten(uploadId) : this.ask(uploadId);
    this.cache.set(uploadId, pending);
    return pending;
  }

  private async askUntilWritten(uploadId: string): Promise<AttachmentPreview | null> {
    let preview = await this.ask(uploadId);
    for (const delay of FRESH_RETRY_DELAYS_MS) {
      if (preview) return preview;
      await new Promise((resolve) => setTimeout(resolve, delay));
      preview = await this.ask(uploadId);
    }
    return preview;
  }

  private ask(uploadId: string): Promise<AttachmentPreview | null> {
    return new Promise((resolve) => {
      this.queue.set(uploadId, resolve);
      if (this.queue.size >= MAX_IDS) {
        this.flush();
      } else if (this.timer === null) {
        this.timer = setTimeout(() => this.flush(), BATCH_WINDOW_MS);
      }
    });
  }

  private flush(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const batch = this.queue;
    this.queue = new Map();
    if (batch.size === 0) return;
    void this.send(batch);
  }

  private async send(batch: Map<string, (preview: AttachmentPreview | null) => void>): Promise<void> {
    let previews: PreviewsResponse['previews'] = {};
    try {
      const response = await firstValueFrom(
        this.http.get<PreviewsResponse>(`${this.baseUrl()}/previews`, {
          params: { ids: Array.from(batch.keys()).join(',') },
        }),
      );
      previews = response.previews ?? {};
    } catch {
      // The cards keep their skeletons; nothing here is worth an error.
    }
    for (const [uploadId, resolve] of batch) resolve(previews[uploadId] ?? null);
  }
}
