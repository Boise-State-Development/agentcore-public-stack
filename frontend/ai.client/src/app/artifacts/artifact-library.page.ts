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
import {
  ArtifactThumbnailComponent,
  type ArtifactThumbnailSource,
} from './components/artifact-thumbnail.component';
import {
  ArtifactShareService,
  type SharedWithMeArtifact,
} from '../session/services/artifacts/artifact-share.service';
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

/** Which slice of the library is on screen. */
export type LibraryTab = 'all' | 'yours' | 'shared';

/**
 * One card, whichever list it came from.
 *
 * The two sources are genuinely different records — a received artifact
 * has no artifact id, no session to go back to, and no `updatedAt` that
 * means anything to the viewer — so they are normalized into one row
 * shape here rather than the template branching on which fields happen
 * to be present. `kind` is what the template branches on, and it is the
 * only thing it needs to branch on: everything else is already resolved.
 */
interface LibraryRow {
  /** Stable `@for` key. Ids come from different key spaces, so they are
   *  prefixed — an artifact id and a share id could otherwise collide
   *  and make Angular reuse one row's DOM for the other. */
  readonly key: string;
  readonly kind: 'owned' | 'shared';
  readonly title: string;
  readonly contentType: string;
  readonly version: number;
  /** What the card mints its preview through — the two kinds are two
   *  different credentials for the same bytes. */
  readonly thumbnail: ArtifactThumbnailSource;
  /** ISO timestamp this row is ordered and labelled by. */
  readonly timestamp: string;
  /** "Updated" for your own, "Shared" for one you received — the words
   *  are not interchangeable and neither are the clocks they read. */
  readonly timestampLabel: string;
  /** Where clicking the card goes. Owner and recipient routes are
   *  different pages, not one page with a mode. */
  readonly route: readonly string[];
  /** Present only on owned rows — the record rename/delete act on. */
  readonly owned?: LibraryArtifact;
  /** Present only on received rows. */
  readonly ownerEmail?: string;
  readonly sessionId?: string;
}

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
  private readonly shares = inject(ArtifactShareService);
  private readonly localSettings = inject(LocalSettingsService);
  private readonly toast = inject(ToastService);
  private readonly router = inject(Router);
  private readonly dialog = inject(Dialog);

  protected readonly items = signal<LibraryArtifact[]>([]);

  /**
   * Artifacts shared with the caller — or `null` when the inbox does not
   * exist in this environment.
   *
   * The distinction is the whole feature flag. `null` means the backend
   * 404'd the endpoint (`ARTIFACT_SHARE_INBOX_ENABLED` off) and the tabs
   * are not rendered at all; `[]` means the inbox is real and empty, and
   * says so. Collapsing the two would either show a permanently empty
   * tab everywhere the feature is off, or hide a real empty state.
   */
  protected readonly received = signal<SharedWithMeArtifact[] | null>(null);
  /** Continuation for the inbox; non-null means there is more to load. */
  protected readonly receivedCursor = signal<string | null>(null);
  protected readonly loadingMore = signal(false);

  protected readonly tab = signal<LibraryTab>('all');
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
    // Both lists: the filter sits above the tabs and applies to whatever
    // is on screen, so offering only the types you happen to own would
    // make it look broken on the "Shared with you" tab.
    for (const row of this.allRows()) {
      const key = this.normalizeType(row.contentType);
      if (!seen.has(key)) {
        seen.set(key, this.styleFor(row.contentType).label);
      }
    }
    return [...seen.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  });

  /** True once the backend has confirmed the inbox exists here. */
  protected readonly sharedAvailable = computed(
    () => this.received() !== null,
  );

  /**
   * "All" leads, because the page's job is to be the one place your
   * artifacts are — splitting them by provenance is a filter on that,
   * not the default reading of it.
   */
  protected readonly tabOptions: ReadonlyArray<{
    value: LibraryTab;
    label: string;
  }> = [
    { value: 'all', label: 'All' },
    { value: 'yours', label: 'Yours' },
    { value: 'shared', label: 'Shared with you' },
  ];

  /** Rows in the current tab before filtering — the denominator in
   *  "n of m", which has to follow the tab or it reads as a bug. */
  protected readonly tabCount = computed(() => this.tabRows().length);

  private readonly ownedRows = computed<LibraryRow[]>(() =>
    this.items().map((item) => ({
      key: `owned:${item.artifactId}`,
      kind: 'owned' as const,
      title: item.title,
      contentType: item.contentType,
      version: item.version,
      thumbnail: {
        kind: 'owned' as const,
        artifactId: item.artifactId,
        version: item.version,
        sessionId: item.sessionId,
        contentType: item.contentType,
      },
      timestamp: item.updatedAt,
      timestampLabel: 'Updated',
      route: ['/artifacts', item.artifactId],
      owned: item,
      sessionId: item.sessionId,
    })),
  );

  private readonly sharedRows = computed<LibraryRow[]>(() =>
    (this.received() ?? []).map((item) => ({
      key: `shared:${item.shareId}`,
      kind: 'shared' as const,
      title: item.title,
      contentType: item.contentType,
      version: item.version,
      thumbnail: {
        kind: 'shared' as const,
        shareId: item.shareId,
        contentType: item.contentType,
      },
      timestamp: item.sharedAt,
      timestampLabel: 'Shared',
      route: ['/shared-artifact', item.shareId],
      ownerEmail: item.ownerEmail,
    })),
  );

  /**
   * Both lists interleaved, newest first.
   *
   * This is the one place the page sorts, and it is a deliberate
   * exception to the rule below: two independently server-ordered lists
   * cannot be shown as one without interleaving them on the client,
   * because there is no single server ordering to preserve. Each list is
   * still consumed in the order the server gave it; only the merge is
   * ours. The tabs that show one list each do not sort at all.
   */
  private readonly allRows = computed<LibraryRow[]>(() =>
    [...this.ownedRows(), ...this.sharedRows()].sort((a, b) =>
      b.timestamp.localeCompare(a.timestamp),
    ),
  );

  /** The rows for the selected tab, before search and type filtering. */
  private readonly tabRows = computed<LibraryRow[]>(() => {
    if (!this.sharedAvailable()) {
      // No inbox here, so there are no tabs and "everything" is yours.
      return this.ownedRows();
    }
    switch (this.tab()) {
      case 'yours':
        return this.ownedRows();
      case 'shared':
        return this.sharedRows();
      default:
        return this.allRows();
    }
  });

  protected readonly filtered = computed<LibraryRow[]>(() => {
    const term = this.search().trim().toLowerCase();
    const type = this.typeFilter();
    return this.tabRows().filter((row) => {
      if (type !== 'all' && this.normalizeType(row.contentType) !== type) {
        return false;
      }
      if (term === '') {
        return true;
      }
      // Sender is searchable on a received row: "who sent me that thing"
      // is at least as likely a starting point as remembering its title.
      return (
        row.title.toLowerCase().includes(term) ||
        (row.ownerEmail ?? '').toLowerCase().includes(term)
      );
    });
  });

  /** Everything the page holds, for the empty-state distinction below. */
  protected readonly totalCount = computed(
    () => this.items().length + (this.received()?.length ?? 0),
  );

  /** True only once loading has resolved, so the empty state can't flash. */
  protected readonly isEmpty = computed(
    () => !this.loading() && !this.error() && this.totalCount() === 0,
  );

  /**
   * "Nothing matches" is a different message from "you have nothing", and
   * conflating them tells a user with a full library that it is empty.
   */
  protected readonly isFilteredEmpty = computed(
    () =>
      !this.loading() &&
      this.totalCount() > 0 &&
      this.filtered().length === 0,
  );

  constructor() {
    void this.load();
  }

  protected async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      // Both in parallel, and the inbox is allowed to fail on its own:
      // `allSettled`, not `all`. Your own library is the page's reason to
      // exist, so an inbox that 503s must not blank it — the tabs simply
      // do not appear, exactly as when the feature is off. The reverse is
      // not true: if your library fails there is nothing to show.
      const [owned, inbox] = await Promise.allSettled([
        this.artifacts.listLibrary(),
        this.shares.listSharedWithMe(),
      ]);

      if (owned.status === 'rejected') {
        throw owned.reason;
      }
      this.items.set(owned.value);

      const page = inbox.status === 'fulfilled' ? inbox.value : null;
      this.received.set(page?.artifacts ?? null);
      this.receivedCursor.set(page?.nextCursor ?? null);
      if (!page && this.tab() === 'shared') {
        // The tab that was selected no longer exists.
        this.tab.set('all');
      }
    } catch {
      this.error.set("We couldn't load your artifacts. Try again in a moment.");
    } finally {
      this.loading.set(false);
    }
  }

  protected setTab(next: LibraryTab): void {
    this.tab.set(next);
  }

  /**
   * Fetch the next page of the inbox and append it.
   *
   * The owned library arrives whole (its endpoint is unpaginated), but
   * the inbox is paged, because a share partition grows by one row per
   * share received and has no ceiling this app controls. So "Load more"
   * is an inbox-only affordance, and it stays visible on the All tab
   * too — the merged list is only as complete as what has been loaded,
   * and hiding the control there would quietly present a partial merge
   * as the whole thing.
   */
  protected async loadMoreShared(): Promise<void> {
    const cursor = this.receivedCursor();
    if (!cursor || this.loadingMore()) {
      return;
    }
    this.loadingMore.set(true);
    try {
      const page = await this.shares.listSharedWithMe(cursor);
      if (!page) {
        // The feature went away under us (a deploy, a flag flip). Drop
        // the tabs rather than leaving a control that does nothing.
        this.received.set(null);
        this.receivedCursor.set(null);
        return;
      }
      this.received.update((rows) => [...(rows ?? []), ...page.artifacts]);
      this.receivedCursor.set(page.nextCursor);
    } catch {
      this.toast.error(
        'Could not load more',
        'The rest of your shared artifacts did not load. Try again in a moment.',
      );
    } finally {
      this.loadingMore.set(false);
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
  protected open(row: LibraryRow): void {
    void this.router.navigate([...row.route]);
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
