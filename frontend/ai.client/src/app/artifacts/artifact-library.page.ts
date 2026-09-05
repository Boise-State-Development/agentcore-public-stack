import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowTopRightOnSquare,
  heroBars3,
  heroChatBubbleLeftRight,
  heroChevronDown,
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroMagnifyingGlass,
  heroPhoto,
  heroSquares2x2,
  heroTableCells,
} from '@ng-icons/heroicons/outline';

import {
  ArtifactHttpService,
  type LibraryArtifact,
} from '../session/services/artifacts/artifact-http.service';
import { LocalSettingsService, type ViewMode } from '../services/local-settings.service';
import { ToastService } from '../services/toast/toast.service';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';

/**
 * Presentation for one artifact content type.
 *
 * Colors are `filetype-*` identity tokens, not brand tokens: a Markdown
 * badge means "this is Markdown" and must not follow a rebrand, the same
 * reason `file-card.component.ts` uses them for attachments. Reusing the
 * same hues keeps one artifact and one uploaded file of the same type
 * reading as the same kind of thing.
 */
interface TypeStyle {
  readonly label: string;
  readonly icon: string;
  readonly bg: string;
  readonly text: string;
}

const TYPE_STYLES: Record<string, TypeStyle> = {
  'text/markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-600 dark:text-filetype-markdown-300',
  },
  'text/x-markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-600 dark:text-filetype-markdown-300',
  },
  'text/html': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-600 dark:text-filetype-code-300',
  },
  'application/xhtml+xml': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-600 dark:text-filetype-code-300',
  },
  'text/csv': {
    label: 'CSV',
    icon: 'heroTableCells',
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-600 dark:text-filetype-sheet-300',
  },
  'image/svg+xml': {
    label: 'SVG',
    icon: 'heroPhoto',
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-600 dark:text-filetype-image-300',
  },
};

const DEFAULT_TYPE_STYLE: TypeStyle = {
  label: 'Text',
  icon: 'heroDocument',
  bg: 'bg-gray-100 dark:bg-gray-700',
  text: 'text-gray-600 dark:text-gray-300',
};

/**
 * The artifact library at `/artifacts` — every artifact the user has ever
 * produced, across every conversation.
 *
 * Artifacts outlive the chat that made them, but until now the only way
 * back to one was to remember which conversation produced it and scroll.
 * This is the index.
 *
 * Filtering and sorting are entirely client-side, which is a deliberate
 * match to the endpoint: it returns the user's whole library in one
 * unpaginated response, because a single user's artifacts sit far under
 * one DynamoDB page. If that endpoint ever gains pagination, search and
 * the type filter have to move to the server with it — a filter that
 * only sees the loaded page is worse than no filter, because it looks
 * authoritative.
 *
 * Ordering comes from the server and is not re-sorted here, for the same
 * reason.
 */
@Component({
  selector: 'app-artifact-library',
  imports: [RouterLink, NgIcon, DatePipe, TooltipDirective],
  templateUrl: './artifact-library.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [
    provideIcons({
      heroArrowTopRightOnSquare,
      heroBars3,
      heroChatBubbleLeftRight,
      heroChevronDown,
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroMagnifyingGlass,
      heroPhoto,
      heroSquares2x2,
      heroTableCells,
    }),
  ],
})
export class ArtifactLibraryPage {
  private readonly artifacts = inject(ArtifactHttpService);
  private readonly localSettings = inject(LocalSettingsService);
  private readonly toast = inject(ToastService);

  protected readonly items = signal<LibraryArtifact[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly search = signal('');
  protected readonly typeFilter = signal<string>('all');
  /** Artifact id whose render URL is being minted, so its button can wait. */
  protected readonly opening = signal<string | null>(null);

  protected readonly viewMode = this.localSettings.artifactsViewMode;

  /**
   * Type options built from what the user actually has, not from the full
   * `TYPE_STYLES` map — offering a "CSV" filter to someone with no CSV
   * artifacts is a dead end that reads as a broken filter.
   */
  protected readonly typeOptions = computed(() => {
    const seen = new Map<string, string>();
    for (const item of this.items()) {
      const key = this.normalizeType(item.contentType);
      if (!seen.has(key)) {
        seen.set(key, this.styleFor(item.contentType).label);
      }
    }
    return [...seen.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  });

  protected readonly filtered = computed(() => {
    const term = this.search().trim().toLowerCase();
    const type = this.typeFilter();
    return this.items().filter((item) => {
      if (type !== 'all' && this.normalizeType(item.contentType) !== type) {
        return false;
      }
      return term === '' || item.title.toLowerCase().includes(term);
    });
  });

  /** True only once loading has resolved, so the empty state can't flash. */
  protected readonly isEmpty = computed(
    () => !this.loading() && !this.error() && this.items().length === 0,
  );

  /**
   * "Nothing matches" is a different message from "you have nothing", and
   * conflating them tells a user with a full library that it is empty.
   */
  protected readonly isFilteredEmpty = computed(
    () =>
      !this.loading() &&
      this.items().length > 0 &&
      this.filtered().length === 0,
  );

  constructor() {
    void this.load();
  }

  protected async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.items.set(await this.artifacts.listLibrary());
    } catch {
      this.error.set("We couldn't load your artifacts. Try again in a moment.");
    } finally {
      this.loading.set(false);
    }
  }

  protected setViewMode(mode: ViewMode): void {
    this.localSettings.setArtifactsViewMode(mode);
  }

  protected onSearch(event: Event): void {
    this.search.set((event.target as HTMLInputElement).value);
  }

  protected onTypeFilter(event: Event): void {
    this.typeFilter.set((event.target as HTMLSelectElement).value);
  }

  protected styleFor(contentType: string): TypeStyle {
    return TYPE_STYLES[this.normalizeType(contentType)] ?? DEFAULT_TYPE_STYLE;
  }

  protected versionLabel(version: number): string {
    return `v${version}`;
  }

  /**
   * Open the rendered artifact in a new tab.
   *
   * The tab is opened *before* the await, then pointed at the minted URL.
   * Opening it afterwards would be a popup blocked by every browser,
   * because by then the click is no longer the active user gesture.
   *
   * Minting is per-click rather than up front for the whole list: a
   * render token is a short-lived (~120s) bearer credential in a URL, so
   * pre-minting one per row would both expire before use and mean a
   * round trip per artifact just to draw the page.
   */
  protected async open(item: LibraryArtifact): Promise<void> {
    const tab = window.open('', '_blank', 'noopener,noreferrer');
    if (!tab) {
      this.toast.error(
        'Pop-up blocked',
        'Allow pop-ups for this site to open artifacts in a new tab.',
      );
      return;
    }

    this.opening.set(item.artifactId);
    try {
      const token = await this.artifacts.mintRenderToken(
        item.artifactId,
        item.version,
        item.sessionId,
      );
      tab.location.href = token.url;
    } catch {
      tab.close();
      this.toast.error(
        'Could not open artifact',
        'The preview link could not be created. Try again in a moment.',
      );
    } finally {
      this.opening.set(null);
    }
  }

  /** Strips the `; charset=…` the writer stores on HTML content types. */
  private normalizeType(contentType: string): string {
    return contentType.split(';')[0].trim().toLowerCase();
  }
}
