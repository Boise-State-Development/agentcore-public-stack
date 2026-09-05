import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DatePipe } from '@angular/common';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroBars3,
  heroChatBubbleLeftRight,
  heroChevronDown,
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroEye,
  heroMagnifyingGlass,
  heroPencilSquare,
  heroPhoto,
  heroSquares2x2,
  heroTableCells,
  heroTrash,
} from '@ng-icons/heroicons/outline';

import {
  ArtifactHttpService,
  type LibraryArtifact,
} from '../session/services/artifacts/artifact-http.service';
import { LocalSettingsService, type ViewMode } from '../services/local-settings.service';
import { ToastService } from '../services/toast/toast.service';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';
import {
  ConfirmationDialogComponent,
  type ConfirmationDialogData,
} from '../components/confirmation-dialog';
import { ArtifactThumbnailComponent } from './components/artifact-thumbnail.component';
import {
  RenameArtifactDialogComponent,
  type RenameArtifactDialogData,
  type RenameArtifactDialogResult,
} from './components/rename-artifact-dialog.component';

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
  imports: [
    RouterLink,
    NgIcon,
    DatePipe,
    TooltipDirective,
    ArtifactThumbnailComponent,
  ],
  templateUrl: './artifact-library.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [
    provideIcons({
          heroBars3,
      heroChatBubbleLeftRight,
      heroChevronDown,
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroEye,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroPhoto,
      heroSquares2x2,
      heroTableCells,
      heroTrash,
    }),
  ],
})
export class ArtifactLibraryPage {
  private readonly artifacts = inject(ArtifactHttpService);
  private readonly localSettings = inject(LocalSettingsService);
  private readonly toast = inject(ToastService);
  private readonly router = inject(Router);
  private readonly dialog = inject(Dialog);

  protected readonly items = signal<LibraryArtifact[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly search = signal('');
  protected readonly typeFilter = signal<string>('all');
  /** Artifact id currently being renamed or deleted, so its row can wait. */
  protected readonly busy = signal<string | null>(null);

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
   * Open an artifact in the in-app viewer.
   *
   * This used to mint a render token and hand it to `window.open`, which
   * made viewing your own artifact contingent on a pop-up — the one thing
   * a browser is entitled to refuse. Anyone with a blocker got a toast
   * instead of their document, and embedded webviews (the Claude Code
   * browser pane among them) refuse `window.open` unconditionally, with
   * or without a feature string, so for them the library was decorative.
   *
   * Navigation cannot be blocked, so opening is now a route change.
   * `/artifacts/:id` mints the token itself and renders through the same
   * `ArtifactViewerComponent` as the docked panel. The new-tab affordance
   * moved into that page, where a refusal is harmless because the
   * artifact is already on screen beside it.
   */
  protected open(item: LibraryArtifact): void {
    void this.router.navigate(['/artifacts', item.artifactId]);
  }

  /**
   * Rename an artifact.
   *
   * The list is patched from the *response*, not from what was typed:
   * the server trims the title, and reconciling against its answer is
   * what keeps this page honest if the two ever diverge.
   */
  protected async rename(item: LibraryArtifact): Promise<void> {
    const data: RenameArtifactDialogData = { title: item.title };
    const dialogRef = this.dialog.open<RenameArtifactDialogResult>(
      RenameArtifactDialogComponent,
      { data },
    );
    const title = await firstValueFrom(dialogRef.closed);
    if (!title) {
      return;
    }

    this.busy.set(item.artifactId);
    try {
      const updated = await this.artifacts.renameArtifact(item.artifactId, title);
      this.items.update((list) =>
        list.map((row) =>
          row.artifactId === updated.artifactId
            ? { ...row, title: updated.title }
            : row,
        ),
      );
      this.toast.success('Artifact renamed');
    } catch {
      this.toast.error(
        'Could not rename artifact',
        'The change was not saved. Try again in a moment.',
      );
    } finally {
      this.busy.set(null);
    }
  }

  /**
   * Delete an artifact after confirmation.
   *
   * The dialog copy names what actually goes — every version, and every
   * share link — because none of that is visible from this page, and a
   * user who shared v2 with a colleague deserves to know the link dies
   * with it. There is no undo, so the row is removed only after the
   * request succeeds.
   */
  protected async confirmDelete(item: LibraryArtifact): Promise<void> {
    const data: ConfirmationDialogData = {
      title: 'Delete this artifact?',
      message:
        `"${item.title || 'Untitled artifact'}" will be deleted permanently, ` +
        'along with every version of it and any share links you have created. ' +
        'This cannot be undone.',
      confirmText: 'Delete',
      cancelText: 'Cancel',
      destructive: true,
    };

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data,
    });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (!confirmed) {
      return;
    }

    this.busy.set(item.artifactId);
    try {
      await this.artifacts.deleteArtifact(item.artifactId);
      this.items.update((list) =>
        list.filter((row) => row.artifactId !== item.artifactId),
      );
      this.toast.success('Artifact deleted');
    } catch {
      this.toast.error(
        'Could not delete artifact',
        'Nothing was removed. Try again in a moment.',
      );
    } finally {
      this.busy.set(null);
    }
  }

  /** Strips the `; charset=…` the writer stores on HTML content types. */
  private normalizeType(contentType: string): string {
    return contentType.split(';')[0].trim().toLowerCase();
  }
}
