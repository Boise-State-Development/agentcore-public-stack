import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroExclamationTriangle,
  heroGlobeAlt,
  heroXMark,
} from '@ng-icons/heroicons/outline';

import { Document } from '../models/document.model';
import {
  CrawlSettings,
  DEFAULT_CRAWL_SETTINGS,
  SINGLE_PAGE_SETTINGS,
} from '../models/web-source.model';
import { WebSourceError, WebSourceService } from '../services/web-source.service';
import { ToastService } from '../../services/toast/toast.service';

/** Data passed in when the assistant editor opens the dialog. */
export interface WebSourceDialogData {
  assistantId: string;
}

/**
 * Modal for adding web content to an assistant's knowledge base.
 *
 * Two modes share one panel:
 *   - "Just this page" (default): URL only. Backend ingests the single page.
 *   - "Crawl linked pages": reveals the depth / pages / concurrency / delay
 *     sliders for a bounded BFS crawl.
 *
 * On submit the modal closes with the pre-created root `Document` so the
 * editor can poll it like a device upload, then starts its watcher loop to
 * surface additional pages as the crawler discovers them.
 */
@Component({
  selector: 'app-web-source-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroExclamationTriangle,
      heroGlobeAlt,
      heroXMark,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'cancel()',
  },
  templateUrl: './web-source-dialog.component.html',
  styles: `
    @import 'tailwindcss';

    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from {
        opacity: 0;
      }
      to {
        opacity: 1;
      }
    }

    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.95);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `,
})
export class WebSourceDialogComponent {
  private readonly dialogRef = inject<DialogRef<Document[]>>(DialogRef);
  private readonly data = inject<WebSourceDialogData>(DIALOG_DATA);
  private readonly webSourceService = inject(WebSourceService);
  private readonly toast = inject(ToastService);

  protected readonly url = signal<string>('');
  protected readonly crawlLinkedPages = signal<boolean>(false);

  protected readonly maxDepth = signal<number>(DEFAULT_CRAWL_SETTINGS.maxDepth);
  protected readonly maxPages = signal<number>(DEFAULT_CRAWL_SETTINGS.maxPages);
  protected readonly concurrency = signal<number>(
    DEFAULT_CRAWL_SETTINGS.concurrency,
  );
  protected readonly minDelay = signal<number>(DEFAULT_CRAWL_SETTINGS.minDelay);
  protected readonly maxDelay = signal<number>(DEFAULT_CRAWL_SETTINGS.maxDelay);

  protected readonly submitting = signal<boolean>(false);
  protected readonly error = signal<string | null>(null);

  protected readonly trimmedUrl = computed(() => this.url().trim());
  protected readonly urlIsValid = computed(() => {
    const value = this.trimmedUrl();
    if (!value) {
      return false;
    }
    try {
      const parsed = new URL(value);
      return parsed.protocol === 'http:' || parsed.protocol === 'https:';
    } catch {
      return false;
    }
  });

  protected readonly canSubmit = computed(
    () => this.urlIsValid() && !this.submitting(),
  );

  protected readonly delayRangeInvalid = computed(
    () => this.maxDelay() < this.minDelay(),
  );

  protected onUrlInput(value: string): void {
    this.url.set(value);
    if (this.error()) {
      this.error.set(null);
    }
  }

  protected setCrawlLinkedPages(value: boolean): void {
    this.crawlLinkedPages.set(value);
  }

  protected setMaxDepth(value: number | string): void {
    this.maxDepth.set(this.toNumber(value, DEFAULT_CRAWL_SETTINGS.maxDepth));
  }

  protected setMaxPages(value: number | string): void {
    this.maxPages.set(this.toNumber(value, DEFAULT_CRAWL_SETTINGS.maxPages));
  }

  protected setConcurrency(value: number | string): void {
    this.concurrency.set(
      this.toNumber(value, DEFAULT_CRAWL_SETTINGS.concurrency),
    );
  }

  protected setMinDelay(value: number | string): void {
    const v = this.toNumber(value, DEFAULT_CRAWL_SETTINGS.minDelay);
    this.minDelay.set(v);
    if (this.maxDelay() < v) {
      this.maxDelay.set(v);
    }
  }

  protected setMaxDelay(value: number | string): void {
    this.maxDelay.set(this.toNumber(value, DEFAULT_CRAWL_SETTINGS.maxDelay));
  }

  protected async submit(): Promise<void> {
    if (!this.canSubmit()) {
      return;
    }
    const url = this.trimmedUrl();
    const settings: CrawlSettings = this.crawlLinkedPages()
      ? {
          maxDepth: this.maxDepth(),
          maxPages: this.maxPages(),
          concurrency: this.concurrency(),
          minDelay: this.minDelay(),
          maxDelay: this.maxDelay(),
          sameDomainOnly: true,
        }
      : SINGLE_PAGE_SETTINGS;

    this.submitting.set(true);
    this.error.set(null);
    try {
      const response = await this.webSourceService.startCrawl(
        this.data.assistantId,
        { url, settings },
      );
      this.dialogRef.close(response.documents);
    } catch (err) {
      this.submitting.set(false);
      const message = this.errorMessage(err);
      this.error.set(message);
      this.toast.error(message);
    }
  }

  protected cancel(): void {
    this.dialogRef.close();
  }

  private toNumber(value: number | string, fallback: number): number {
    const n = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  private errorMessage(err: unknown): string {
    if (err instanceof WebSourceError) {
      return err.message;
    }
    if (err instanceof Error) {
      return err.message;
    }
    return 'Something went wrong. Try again.';
  }
}
