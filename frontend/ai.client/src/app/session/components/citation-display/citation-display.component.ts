import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { Citation } from '../../services/models/message.model';

/** A crawled web page a citation came from, ready to link to. */
export interface CitationPage {
  /** The page's address (http or https only). */
  url: string;
  /** Host and path, for the line under the title. */
  label: string;
}

export interface CitationItem {
  citation: Citation;
  /** The filename, or a web page's title without the `.md` the crawler adds. */
  title: string;
  page: CitationPage | null;
}

/**
 * The page a citation links to, or null. The server only sends http(s)
 * addresses; this checks again so nothing else can become a link.
 */
export function citationPage(sourceUrl: string | undefined): CitationPage | null {
  if (!sourceUrl) return null;
  let parsed: URL;
  try {
    parsed = new URL(sourceUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  const path = parsed.pathname.replace(/\/+$/, '');
  return { url: parsed.href, label: parsed.host + path };
}

function toItem(citation: Citation): CitationItem {
  const page = citationPage(citation.sourceUrl);
  const title = page ? citation.fileName.replace(/\.md$/i, '') || citation.fileName : citation.fileName;
  return { citation, title, page };
}
import { DocumentService } from '../../../assistants/services/document.service';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

@Component({
  selector: 'app-citation-display',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SpinnerComponent],
  host: {
    '(document:keydown.escape)': 'onEscapeKey()',
  },
  template: `
    @if (citationCount() > 0) {
      <div
        class="relative inline-block"
        (mouseenter)="onMouseEnter()"
        (mouseleave)="onMouseLeave()"
        role="region"
        [attr.aria-label]="'Citations: ' + citationCount() + ' sources'"
      >
        <!-- Badge matching other metadata chips -->
        <button
          type="button"
          class="inline-flex min-h-7 items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium
                 bg-gray-100 text-primary-accessible hover:bg-gray-200
                 dark:bg-gray-700 dark:text-primary-50 dark:hover:bg-gray-600 dark:hover:text-white
                 transition-colors motion-reduce:transition-none
                 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          [attr.aria-expanded]="isExpanded()"
          [attr.aria-controls]="isExpanded() ? 'citation-listbox' : null"
          aria-haspopup="listbox"
          (keydown)="onBadgeKeydown($event)"
        >
          <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
          </svg>
          <span>{{ citationCount() }} {{ citationCount() === 1 ? 'source' : 'sources' }}</span>
        </button>

        <!-- Expanded Citation List Popover -->
        @if (isExpanded()) {
          <div
            id="citation-listbox"
            class="absolute bottom-full left-0 z-50 mb-2 w-80 max-h-72 overflow-y-auto
                   rounded-lg border border-gray-200 bg-white shadow-lg
                   dark:border-gray-700 dark:bg-gray-800
                   sm:w-96"
            role="listbox"
            aria-labelledby="citation-header"
          >
            <!-- Header -->
            <div class="sticky top-0 border-b border-gray-200 bg-gray-50 px-4 py-2.5 dark:border-gray-700 dark:bg-gray-900">
              <h3 id="citation-header" class="text-sm font-semibold text-gray-900 dark:text-gray-100">
                Source Documents
              </h3>
            </div>

            <!-- Citation Items -->
            <div class="divide-y divide-gray-100 dark:divide-gray-700">
              @for (item of items(); track $index; let i = $index) {
                @let citation = item.citation;
                <div
                  class="group px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700
                         transition-colors motion-reduce:transition-none
                         focus-within:bg-gray-50 focus-within:ring-2 focus-within:ring-inset focus-within:ring-primary-500 dark:focus-within:bg-gray-700"
                  role="option"
                  [attr.aria-selected]="false"
                >
                  <div class="flex items-start gap-3">
                    <!-- Citation Number -->
                    <span
                      class="flex size-6 shrink-0 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold text-primary-accessible dark:bg-gray-700 dark:text-primary-50"
                      aria-hidden="true"
                    >
                      {{ i + 1 }}
                    </span>

                    <!-- Citation Content -->
                    <div class="min-w-0 flex-1">
                      <!-- Filename, or a web page's title and address -->
                      <p class="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
                        {{ item.title }}
                      </p>
                      @if (item.page) {
                        <p class="truncate text-xs text-gray-500 dark:text-gray-400" data-testid="citation-page-address">
                          {{ item.page.label }}
                        </p>
                      }

                      <!-- Text excerpt -->
                      <p class="mt-1 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                        {{ citation.text }}
                      </p>

                      <!-- A crawled web page links to the page itself. It is a public
                           address, not the author's file, so the #111 download switch
                           does not withhold it (show_citations still does, server-side). -->
                      @if (item.page) {
                      <a
                        [href]="item.page.url"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary-accessible hover:text-primary-700 hover:underline
                               dark:text-primary-accessible-dark dark:hover:text-primary-300
                               focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                      >
                        <svg class="size-3" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">
                          <path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                        </svg>
                        <span>Open page</span>
                        <span class="sr-only">(opens in new tab)</span>
                      </a>
                      } @else if (allowDownload()) {
                      <!-- Download button (#111: hidden when downloads are disabled) -->
                      <button
                        type="button"
                        class="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary-accessible hover:text-primary-700 hover:underline
                               dark:text-primary-accessible-dark dark:hover:text-primary-300
                               focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500
                               disabled:opacity-50 disabled:cursor-not-allowed disabled:no-underline"
                        [disabled]="loadingDocumentId() === citation.documentId"
                        (click)="onDownloadClick(citation)"
                      >
                        @if (loadingDocumentId() === citation.documentId) {
                          <app-spinner size="sm" label="Loading" />
                          <span>Loading...</span>
                        } @else {
                          <svg class="size-3" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
                          </svg>
                          <span>Download source</span>
                          <span class="sr-only">(opens in new tab)</span>
                        }
                      </button>
                      }
                    </div>
                  </div>
                </div>
              }
            </div>
          </div>
        }
      </div>
    }
  `,
  styles: `
    @reference "../../../../styles/theme.css";

    :host {
      display: inline-block;
    }
  `,
})
export class CitationDisplayComponent {
  private readonly documentService = inject(DocumentService);

  citations = input<Citation[]>([]);
  /**
   * #111: when false, the "Download source" button is hidden on every citation card.
   * The citations themselves still render (filename + excerpt) — only the download
   * affordance is withheld. Defaults true so nothing changes for existing agents.
   */
  allowDownload = input<boolean>(true);
  isExpanded = signal<boolean>(false);

  // Track which document is currently loading
  loadingDocumentId = signal<string | null>(null);

  // Computed for derived state - avoids multiple signal reads in template
  citationCount = computed(() => this.citations().length);

  /** Each citation with what its card shows: a title, and a page to open when it has one. */
  items = computed(() => this.citations().map(toItem));

  private collapseTimeout: ReturnType<typeof setTimeout> | null = null;

  /**
   * Handle download button click - fetches presigned URL on demand
   */
  async onDownloadClick(citation: Citation): Promise<void> {
    if (!citation.assistantId || !citation.documentId) {
      console.error('Citation missing assistantId or documentId');
      return;
    }

    try {
      this.loadingDocumentId.set(citation.documentId);

      const response = await this.documentService.getDownloadUrl(
        citation.assistantId,
        citation.documentId
      );

      // Open the presigned URL in a new tab
      window.open(response.downloadUrl, '_blank', 'noopener,noreferrer');
    } catch (error) {
      console.error('Failed to get download URL:', error);
      // Could show a toast notification here
    } finally {
      this.loadingDocumentId.set(null);
    }
  }

  onMouseEnter(): void {
    if (this.collapseTimeout) {
      clearTimeout(this.collapseTimeout);
      this.collapseTimeout = null;
    }
    this.isExpanded.set(true);
  }

  onMouseLeave(): void {
    this.collapseTimeout = setTimeout(() => {
      this.isExpanded.set(false);
    }, 300);
  }

  onEscapeKey(): void {
    if (this.isExpanded()) {
      this.isExpanded.set(false);
      if (this.collapseTimeout) {
        clearTimeout(this.collapseTimeout);
        this.collapseTimeout = null;
      }
    }
  }

  onBadgeKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      this.isExpanded.set(!this.isExpanded());
      if (this.collapseTimeout) {
        clearTimeout(this.collapseTimeout);
        this.collapseTimeout = null;
      }
    }
  }
}
